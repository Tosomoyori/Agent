# 架构决策记录（ADR）

这里记录的是**做过的技术决策**：问题是什么、有哪些选项、选了什么、**代价是什么**。

ADR 记录的是决策过程：设计文档说明系统当前形态，ADR 保留各种被否决的替代方案
及其否决理由。评审与后续维护中需要追溯的通常是后者。

每份 ADR 均包含「代价」一节。未记录代价的决策通常意味着评估不充分。

| 编号 | 决策 | 一句话 |
| --- | --- | --- |
| [0001](0001-content-block-messages.md) | 内部消息用内容块模型 | 为了转 Anthropic 是直接映射，代价是自己写双向转换和配对不变量校验 |
| [0002](0002-typed-event-stream.md) | 引擎只产出类型化事件 | 四个消费者共享一条流；意外收获是追踪变成了纯函数 |
| [0003](0003-native-tool-calling.md) | 原生 function calling | 文本解析式 ReAct 有四个静默降级点，且表达不了并行调用 |
| [0004](0004-stream-normalization.md) | 流式增量在适配器层归一化 | 实测证伪了社区流传的「累积重发」说法；留了兜底但要求留痕 |
| [0005](0005-session-persistence.md) | 会话持久化自造 | 不满足委托 Temporal 的前提；明确承认 run 级恢复没做 |
| [0006](0006-command-policy.md) | 命令安全用三分裁决 | 黑名单必然漏也会误杀；并明确承认允许清单不是沙箱 |
| [0007](0007-retry-policy.md) | 重试只在首个 chunk 之前 | 已吐出的内容收不回来；SDK 重试必须关掉否则次数相乘 |
| [0008](0008-evaluation-metrics.md) | 主指标用 pass^k 和状态判据 | pass@k 衡量潜力，pass^k 衡量可靠性，上线的关键是后者 |
| [0009](0009-from-scratch-versus-framework.md) | 自研内核 + LangChain 适配层 | 「自研」推不出「熟悉框架」，这是两个主张；适配层还是抽象设计的试金石 |

## 相关的其他文档

- [docs/provider-notes.md](../provider-notes.md) —— 实测出来的 provider 流式行为
- [docs/evaluation.md](../evaluation.md) —— 评测设置、结果、判据设计约束与局限
- [docs/memory-design.md](../memory-design.md) —— 记忆系统的实现范围与取舍
- [docs/a2a-scope.md](../a2a-scope.md) —— Agent 发现能力的实现范围与取舍
