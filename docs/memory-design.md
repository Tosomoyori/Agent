# 记忆系统：实现范围与取舍

本文档说明记忆系统的实现范围，以及未实现部分的评估结论与后续路径。

## 已实现

代码位于 [src/agentkit/memory/](../src/agentkit/memory/)。

| 机制 | 位置 | 解决什么问题 |
| --- | --- | --- |
| Token 预算裁剪 | [`working.py`](../src/agentkit/memory/working.py) `WorkingMemory.assemble` | 在上下文超出窗口前主动丢弃最早的对话 |
| 原子配对丢弃 | 同上 `_take_droppable_prefix` | 工具调用组整组丢弃，避免产生孤儿块 |
| 摘要压缩 | 同上 `summarizer` 钩子 | 被裁掉的历史经模型总结后保留 |
| 会话持久化 | [`store.py`](../src/agentkit/memory/store.py) | 跨进程保留对话 |
| 唯一装配入口 | [`manager.py`](../src/agentkit/memory/manager.py) | 「发给模型的消息序列」由单点决定 |

### 原子配对丢弃

这是裁剪逻辑中最关键的一条约束。

`tool_use` 与其 `tool_result` 必须成对出现。裁剪历史时只丢弃其中一半，下一次请求
会被 API 拒绝（Anthropic 报错明确；OpenAI 系表现为模型输出质量显著下降）。
因此 [`_take_droppable_prefix`](../src/agentkit/memory/working.py) 中，
当一组消息只能丢弃一半时，结论是**一半都不丢弃**。

该不变量由 [`validate_conversation`](../src/agentkit/core/types.py) 在引擎每一轮
开头校验，两者配套。落盘前同样校验一次：取消或出错时消息序列可能残缺，
写入存储会导致下一轮开场即违反不变量。

## 未实现的部分

以下能力属于记忆系统的深水区，在当前场景下的评估结论是收益不足以抵消引入的复杂度。

### 业界通行做法

记忆处理的事实标准是一条流水线：

```
抽取 (extract) → 整合 (consolidate) → 存储 (store) → 检索 (retrieve)
```

分层划分：

| 层 | 内容 | 典型实现 |
| --- | --- | --- |
| Working | 当前上下文窗口内的对话 | checkpointer / session（本系统已实现） |
| Episodic | 过去交互的摘要与示例 | 会话摘要 + 向量召回 |
| Semantic | 事实与偏好（用户画像、知识三元组） | 向量库 / 知识图谱 |
| Procedural | 行为规则 | 通常实现为对 system prompt 的优化，而非检索 |

工业界落地最多的是**向量检索 + 会话摘要**（mem0 社区规模最大；
ChatGPT memory 与 Microsoft Foundry 的托管实现均为此路线）。
知识图谱（Zep / Graphiti 的时序事实建模）属于长尾，主要用于需要
「某一时间点上什么为真」的场景。

### 语义记忆：未实现

**目标能力**：跨会话记住用户身份与偏好。

**现状**：会话持久化已覆盖同一会话内跨轮次的信息保留。

**完整实现所需的组件**：

1. 嵌入模型（本地部署需拉取权重，走 API 则产生额外成本）；
2. 向量库依赖；
3. 事实抽取提示词；
4. 去重与冲突消解逻辑。

其中 3、4 是主要难点，且**缺乏可接受的评测方式**——「记住了多少」难以客观衡量，
常见的做法是人工抽查少数样例，不足以支撑量化结论。

### 情节记忆：未实现（且不应作为独立模块）

在「向量检索 + 会话摘要」架构下，情节记忆的实现形式是存储中 `type="episode"`
的条目，不构成独立子系统。单独建模块属于过度设计。

### 记忆整合：未实现

**成熟做法**：去重（嵌入相似度 + 实体重叠，LLM 裁决）→ 矛盾消解（标记
`SUPERSEDED` 而非物理删除）→ 异步 sweep。

**依赖前提**：每一步都需要嵌入模型与调度器。在语义记忆未实现的前提下，
整合没有可整合的对象。

### Procedural memory：未实现（不属于记忆系统）

其本质是 prompt 优化问题，与检索无关，归入记忆系统属于分类错误。

## 后续实现路径

按依赖顺序，每步可独立验证：

1. **嵌入与向量检索**：为 `SessionStore` 增加 `facts` 表
   （`content` / `embedding` / `type` / `valid_from` / `superseded_by`），
   召回时使用向量相似度与关键词的混合检索。完成后即可对比开 / 关记忆的评测指标。
2. **抽取与冲突消解**：在被裁剪的历史上执行抽取，产出候选事实；
   对每条候选执行 `ADD / UPDATE / DELETE / NOOP` 裁决；
   矛盾时标记旧条目的 `superseded_by` 而非物理删除，保留可审计性。
3. **异步 sweep**：会话结束后使用低成本模型执行整合，不阻塞主流程。

**验收标准**：以仓库自带的评测（见 [docs/evaluation.md](evaluation.md)）比较
开 / 关记忆时的任务成功率与单位成本。该收益必须以数据支持。

## Token 估算的精度取舍

[`estimate_tokens`](../src/agentkit/memory/working.py) 按字符数估算
（CJK 约 0.6 token/字，其余约 0.3 token/字符），未使用真实 tokenizer。

依据与代价：

- DeepSeek 提供了 demo tokenizer，但非官方维护库，纳入依赖不划算；
- 无官方 count_tokens 接口（Anthropic 与 Gemini 提供，OpenAI 与 DeepSeek 不提供）；
- 估算精度对**裁剪**足够——裁剪本身应保留安全余量，高估只会少保留几条历史；
- **计费一律以服务端返回的 `usage` 为准**，不使用估算值。

若后续接入 Anthropic，可使用其 `count_tokens` 接口获取精确值，
届时 `estimate_tokens` 应退化为无接口可用时的兜底实现。

## 参考

- [mem0 论文 (arXiv:2504.19413)](https://arxiv.org/abs/2504.19413)
- [MemGPT (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560)
- [Zep 时序知识图谱 (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956)
- [Chroma 上下文衰减研究](https://research.trychroma.com/context-rot)：8K→128K
  输入长度区间普遍下降 15–30%，为「裁剪 + 摘要」方案提供实证依据
