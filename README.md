# AgentKit

从零实现的 Agent 开发框架。核心循环、工具层、记忆、评测、可观测性全部自研，不依赖 LangChain 等现成框架。

> **状态**：Phase 1 进行中 —— 原生 tool calling 内核 + CLI 已可用。服务化、可观测性、评测见下方路线图。

## 为什么又造一个轮子

这个项目最初是一个约 500 行的单文件 ReAct agent：正则从模型输出里抠 JSON、把工具结果伪装成 `role: "user"` 的文本、用正则黑名单拦危险命令、用 `input()` 阻塞式确认、模块级全局变量存工作区。它能跑，但每一处都挡住了它变成可上线系统的路。

重写针对的具体问题逐条列在 [legacy/README.md](legacy/README.md)。

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
```

## 设计要点

| 决策 | 原因 | 详见 |
| --- | --- | --- |
| 内部消息用**内容块模型**，而非 OpenAI 的 `tool_calls[]` + `role:"tool"` | Anthropic 没有 `tool` role，`tool_use`/`tool_result` 是消息内的 content block 且必须严格相邻配对。内部规范成 block 后转 Anthropic 是直接映射 | — |
| **原生 function calling** 取代文本解析 ReAct | 文本解析的格式错误率在业界是 17–23%，且无法表达并行工具调用 | — |
| 引擎只 **yield 类型化事件** | SSE、CLI、可观测、评测四个消费者共享同一条流，避免「返回值 + 回调」双真相源 | — |
| 工具参数用 **pydantic 校验，失败回灌给模型** | 让模型看见自己的参数错误并自我修正，而不是抛一个字符串了事 | — |
| 路径边界用 **`realpath` + `commonpath`** | `abspath` + `startswith` 不解析符号链接，工作区内一个 symlink 就能绕过 | — |

## 项目结构

```
src/agentkit/
├── core/       # 零依赖层：消息模型、事件、错误、配置
├── llm/        # Provider 抽象与适配（OpenAI 兼容 / Anthropic）
├── tools/      # 工具协议、注册表、安全策略、内置工具
├── memory/     # 工作记忆与持久化
├── runtime/    # 引擎循环、预算、取消、检查点
├── observability/  # span 树与导出
├── evaluation/ # 评测数据集、指标、运行器、报告
├── discovery/  # AgentCard 注册与发现
└── app/        # CLI 与 HTTP 服务
```

依赖方向是单向的：`core` 零依赖；`llm`/`tools`/`observability` 只依赖 `core`；`runtime` 依赖全部；`app` 只依赖 `runtime`。**`tools` 绝不 import `runtime`**（审批接口由 runtime 注入实现）。

## 路线图

- [x] **Phase 0** 地基：依赖声明、环境、包结构
- [x] **Phase 1** 自研内核 + 原生 tool calling + CLI
- [ ] **Phase 2** 流式、工具策略与异步审批、预算、取消、工作记忆
- [ ] **Phase 3** 可观测性（`gen_ai.*` span）+ FastAPI SSE 服务 + Web 控制台 + Agent 发现
- [ ] **Phase 4** 评测：任务成功率 / 工具正确性 / pass^k / 成本
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
