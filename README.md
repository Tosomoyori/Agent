# AgentKit

从零实现的 Agent 开发框架。核心循环、工具层、记忆、评测、可观测性全部自研，不依赖 LangChain 等现成框架。

> **状态**：全部阶段完成 —— 内核、流式、取消、预算、审批、记忆、可观测性、
> HTTP 服务、Web 控制台、Agent 发现与评测。

## 评测数字

`uv run agentkit eval eval_suites --mode live --trials 5` —— 22 个用例 × 5 次
= 110 次真实调用（`deepseek-flash`）：

| 指标 | 值 |
| --- | --- |
| 任务成功率 / pass@5 / pass^5 | 100% / 100% / 100% |
| 工具调用准确率 | 100% |
| 平均步数 | 2.95 |
| 延迟 P50 / P95 | 2932 ms / 4677 ms |
| 每次成功成本 | 0.0008 CNY |

**这个 100% 的正确读法是「基线可复现、零失败」，不是「agent 完美」。**
22 个用例对当前模型偏容易，不具区分度。完整的诚实解读、以及评测过程中暴露出的
**我自己的判据错了五次**的记录，见 [docs/evaluation-notes.md](docs/evaluation-notes.md)——
那份文档比这里的数字更有价值。

## 设计取舍

从零实现，不套 LangChain / LlamaIndex。目标是能把 Agent 的每个机制讲清楚，而不是会用某个
框架的 API。代价是持久化、可观测、重试这些都要自己写——这个取舍是清醒的，不是没评估过。

几个关键决策：

| 决策 | 原因 |
| --- | --- |
| 内部消息用**内容块模型**（`TextBlock` / `ToolUseBlock` / `ToolResultBlock`），而非 OpenAI 的 `tool_calls[]` + `role:"tool"` | Anthropic 没有 `tool` role，它的 `tool_use`/`tool_result` 是消息内的 content block，且同一轮的每个调用都必须在紧随其后的消息里配对。内部规范成 block 后转 Anthropic 是直接映射；反过来则每次都要重新分组重排 |
| **原生 function calling** 取代文本解析 | 文本解析的格式错误率在业界是 17–23%，而且表达不了并行工具调用 |
| 引擎只 **yield 类型化事件** | SSE、CLI、可观测、评测四个消费者共享同一条流，避免「返回值 + 回调」的双真相源 |
| 工具参数用 **pydantic 校验，失败回灌给模型** | 让模型看见自己的参数错误并自我修正，而不是抛个字符串了事 |
| 路径边界用 **`realpath` + `commonpath`** | 字符串前缀比较不解析符号链接，工作区内一个 symlink 就能写到外面；`C:\foo` 也不是 `C:\foobar` 的父目录 |
| 命令安全用**三分裁决**（拒绝 / 放行 / 转审批）而非正则黑名单 | 黑名单必然漏（`rm -r -f`、`rm${IFS}-rf`），还会误杀（`echo "rm -rf /"` 只是打印字符串）。默认不信任，看不明白的一律转人工 |
| 引擎事件经**队列**而非直接 yield | 审批事件必须在工具**仍然阻塞等待**时就送达客户端，否则就成了「先等出结果再问你要不要批准」 |
| 工具结果失败一律**回灌**而不中断 run | 参数写错、策略拒绝都是模型能自我修正的；把它们变成可读的错误结果，比抛异常让整个 run 失败有用 |
| 流式重试**只在首个 chunk 之前**允许 | 已经吐给调用方的内容收不回来，重来一次会重复输出且那部分输入 token 已计费 |
| 记忆**只做工作记忆 + 持久化** | 语义记忆/情节记忆需要嵌入模型与向量库，且缺乏可接受的评测方式。取舍与后续路径写在 [docs/memory-design.md](docs/memory-design.md) |
| 可观测性是事件流的**消费者**，不往引擎里埋点 | 埋点式的成对调用迟早会漏掉一处（返回值没接住、异常分支没关），trace 里就出现永不结束的 span。作为消费者，追踪逻辑还能脱离引擎单独测试 |
| A2A 只做**卡片 + 注册发现** | 签名卡片与 JSON-RPC 服务端是协议一致性工作，不是能力工作，且在控制台里展示不出来。取舍见 [docs/a2a-scope.md](docs/a2a-scope.md) |
| 评测判据**优先用正面事实**，避免 `answer_not_contains` | 负面判据检查的是措辞不是事实，会惩罚正确回答。第一版判据连错五次，记录在 [docs/evaluation-notes.md](docs/evaluation-notes.md) |
| 评测**分 hermetic / live 两层** | hermetic 用假模型回放，验证评测框架自身（CI 可跑、零成本）；agent 的真实能力只能在 live 模式测 |

## 快速开始

需要 [uv](https://docs.uv.io/)。Python 版本由 `.python-version` 固定，无需手动准备。

```bash
uv sync --extra dev          # 建 .venv 并装依赖
cp .env.example .env         # 填入 DEEPSEEK_API_KEY
uv run agentkit --help
```

跑一个任务：

```bash
uv run agentkit run "列出当前目录的文件，然后告诉我 README.md 有多少行"

uv run agentkit run "读一下 README" --session s1   # 带会话，跨轮次记住上下文
uv run agentkit chat                                # 交互式对话
uv run agentkit serve                               # 起 HTTP 服务 + Web 控制台
uv run agentkit sessions list                       # 查看历史会话
uv run agentkit tools                               # 列出工具及其参数 schema

uv run agentkit eval eval_suites --mode hermetic    # 评测框架自检，不花钱
uv run agentkit eval eval_suites --mode live -n 5   # 真实评测，会产生费用
```

起服务后：

- 控制台 http://127.0.0.1:8000/ —— 实时看推理链路、工具调用、审批请求与 trace 树
- Agent 卡片 http://127.0.0.1:8000/.well-known/agent-card.json
- trace 落在 `.agentkit/traces.jsonl`

需要审批的动作（写文件、执行未知命令）会在终端里问一句；拒绝之后模型会收到
一条明确的错误，把这一步交回给人，而不是反复重试。非交互场景加 `--no-approve`
直接拒绝所有需要审批的动作——**默认拒绝，不是默认放行**。

## 项目结构

```
src/agentkit/
├── core/       # 零依赖层：消息模型、事件、错误、配置、用量
├── llm/        # Provider 抽象、流式归一化、重试
├── tools/      # 工具协议、注册表、安全策略、内置工具
├── memory/     # 工作记忆与 SQLite 持久化
├── runtime/    # 引擎循环、事件总线、预算、取消、审批
├── observability/  # span 树与导出（对齐 OTel GenAI 约定）
├── discovery/  # AgentCard 注册与发现（对齐 A2A）
├── evaluation/ # 评测数据集、判据、指标、运行器、报告
└── app/        # CLI、HTTP 服务（SSE）与 Web 控制台
```

依赖方向是单向的：`core` 零依赖；`llm`/`tools`/`observability` 只依赖 `core`；`runtime` 依赖全部；`app` 只依赖 `runtime`。**`tools` 绝不 import `runtime`**（审批接口由 runtime 注入实现）。

## 路线图

- [x] **Phase 0** 地基：依赖声明、环境、包结构
- [x] **Phase 1** 自研内核 + 原生 tool calling + CLI
- [x] **Phase 2** 流式、工具策略与异步审批、预算、取消、工作记忆
- [x] **Phase 3** 可观测性（`gen_ai.*` span）+ FastAPI SSE 服务 + Web 控制台 + Agent 发现
- [x] **Phase 4** 评测：任务成功率 / 工具正确性 / pass^k / 成本
- [ ] **Phase 5** 文档、ADR、压测数据
- [ ] **Phase 5** 文档、ADR、压测数据

## 开发

```bash
uv run pytest                # 单测（全部离线，用 fake model）
uv run pytest -m live        # 真实调用 LLM 的冒烟测试（会产生费用）
uv run ruff check .          # 静态检查
```

### 测试怎么让 Agent 的行为可复现

模型输出不确定，但 Agent 的行为可以是确定的——**把不确定性收敛到 `ChatModel` 这一个
边界上**。测试里放一个按脚本返回预设响应的假模型（`tests/conftest.py` 的
`ScriptedModel`），引擎循环、工具执行、消息转换、事件序列就全部变成确定性逻辑，
不碰网络、不花钱、结果可复现。

真实模型只在两处出现：`-m live` 的冒烟测试，以及需要录 provider 协议行为的 fixture。

## 能力边界

刻意没做的事，以及理由，写在 `docs/adr/` 里。宁可说清楚「这个我知道但评估后没做」，也不放一个半成品进去。
