# ADR 0009：自研内核 + LangChain 适配层

**状态**：已采纳

## 问题

Agent 开发框架（LangChain / LangGraph、LlamaIndex、CrewAI 等）已相当成熟。
本项目应当基于现有框架构建，还是从零实现？

## 前提：两个独立的主张

**「实现过框架」与「熟悉某个框架」是两个不同的主张，前者推不出后者。**

| | 自研能证明 | 自研不能证明 |
| --- | --- | --- |
| 机制理解 | ReAct 循环、消息模型、取消传播、崩溃恢复的实现方式 | — |
| 生态经验 | — | `Runnable` 协议、`BaseChatModel` 契约、tool binding、LCEL |

可以独立实现 agent 循环而从未使用过 LangChain 的 `bind_tools`，反之亦然。
因此「纯自研」方案在生态经验一项上没有产出。

## 选项

**A. 基于 LangGraph / LangChain 构建。** 开发快，生态现成，可直接使用框架名。

**B. 纯自研，不引入框架。**

**C. 自研内核 + 薄适配层。** 核心自行实现，另外提供与 LangChain 的双向互操作。

## 决定

选 **C**。

## 理由

### 否决 A

使用 LangGraph 可获得的是「如何使用该框架」的经验，无法获得其调度细节的经验：
checkpointer 的落盘时机、`interrupt` 的恢复语义、状态图的合并规则。
这些恰是深入评审时会被追问的部分。

在框架之上构建证明的是**会用**，从零构建证明的是**懂机制**。
本项目的目标是验证后者。

### 否决 B

除「生态经验」一项无产出外，纯自研还有一个隐性代价：
**缺少参照物，无法判断自身抽象设计的质量。**

### 采用 C 的收益

**双向收益：**

- 自研内核 → 机制层面可追溯（见 ADR 0001–0008，均为实现层面的决策）；
- 适配层 → 生态可接入，且「熟悉框架」有**可验证**的证据：
  [`integrations/langchain.py`](../../src/agentkit/integrations/langchain.py)
  有 28 个测试，覆盖 `bind_tools`、LCEL 管道、以及被 LangChain agent 调用。

**此外，适配层构成对抽象设计的检验。**

若 `ChatModel` / `ToolSpec` 的抽象足够清晰，包装一层 LangChain 接口应当只需做
**格式转换**，无需修改任何核心代码。若需要修改 `runtime` 或 `core` 才能适配，
则说明抽象存在缺陷。

实际结果：该层只 import 了 `agentkit.core` / `agentkit.llm` / `agentkit.tools`，
**未涉及 `runtime`**。这是分层设计成立的可验证证据。

## 代价

- **额外维护成本**。LangChain 版本演进较快（当前为 1.4.x，langchain-core 1.6.x），
  适配层需跟进；
- **适配存在信息损耗**。LangChain 的工具没有「注入上下文」概念，因此 AgentKit 的
  `ToolContext` 在转换时被固定为一个具体工作区
  （见 `to_langchain_tools` 的 `workspace` 参数）。该映射是有损的，已在文档中说明；
- **可选依赖也是依赖**。因此实现为 extra（`uv sync --extra langchain`），
  核心包零 LangChain 依赖，`importorskip` 保证未安装该 extra 时测试套件不受影响。

## 相关

- 适配层的双向能力与三个映射差异见
  [`integrations/langchain.py`](../../src/agentkit/integrations/langchain.py) 的模块文档
- 可运行示例：[examples/langchain_interop.py](../../examples/langchain_interop.py)
