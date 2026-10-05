# Agent 发现能力：实现范围与取舍

本文档说明 Agent 注册与发现能力的实现范围，以及未实现部分的评估结论。

## 背景：两条互补的协议

Agent 互操作在 2026 年已收敛出两条事实标准：

| 协议 | 管什么 | 定位 |
| --- | --- | --- |
| **MCP** (Model Context Protocol) | agent ↔ 工具/数据 | 垂直：单个 agent **内部**如何使用工具 |
| **A2A** (Agent2Agent) | agent ↔ agent | 水平：一个 agent 如何发现并调用**另一个** agent |

A2A 由 Google 于 2025 年 4 月发起，同年 6 月捐赠给 Linux Foundation，
2026 年 8 月转入 Agentic AI Foundation（与 MCP 同属一个基金会，
各自保留 maintainer 与发布节奏）。核心机制有三：

1. **Agent Card**：agent 的自述文件，位于约定的 `/.well-known/agent-card.json`；
2. **能力发现**：客户端拉取卡片，解析 `skills[]` / `capabilities` / `securitySchemes`；
3. **任务交换**：JSON-RPC 2.0 over HTTP，流式使用 SSE，异步使用 webhook 推送。

第 2 条是关键：它将「到何处查询一个服务能做什么」标准化为一个约定 URL。
缺少该约定时，每个 agent 平台都需要自行发明服务目录。

## 已实现

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| AgentCard 数据模型 | [`discovery/card.py`](../src/agentkit/discovery/card.py) | 字段对齐 A2A，`to_well_known()` 负责 camelCase 转换 |
| 注册 / 管理 / 发现 | [`discovery/registry.py`](../src/agentkit/discovery/registry.py) | 注册、注销、按标签检索、歧义检测 |
| `/.well-known/agent-card.json` | [`app/api.py`](../src/agentkit/app/api.py) | 由 FastAPI 挂载 |
| `/agents` 目录接口 | 同上 | 列出注册表内容，支持按标签过滤 |
| 技能由工具推导 | 同上 `_skills_for` | 卡片的 `skills[]` 从**实际注册的工具**生成 |

**技能从工具推导**是刻意的设计：手写技能清单容易产生漂移——新增工具后忘记更新卡片，
发现机制会把该 agent 无法完成的任务派发过来。从 `registry.names()` 推导虽朴素，
但不存在漂移可能。

## 未实现的部分

### 签名 Agent Card（JCS + Ed25519 detached JWS）

A2A v1.0 的主要特性：使用 RFC 8785 的 JSON 规范化与 RFC 8037 的 Ed25519 签名，
保护卡片的**身份层**（不保护声明层）。未实现的理由：

- 实现量可观（规范化、密钥管理、JWKS 端点、验签）；
- 在控制台演示中不可见——签名正确与否无法通过观察判断；
- 其解决的问题（跨组织的 agent 身份信任）在「单机运行若干 agent」的场景中不存在。

需要说明的是：v1.0 用吊销式 JWS 保护身份层而非声明层，这一选择是合理的——
声明可以随时变更，身份必须稳定。

### JSON-RPC 2.0 服务端与 `tasks/*` 方法族

A2A 的任务交换定义了完整方法集：`message/send`、`message/stream`、`tasks/get`、
`tasks/list`、`tasks/cancel`、`tasks/resubscribe`，以及一组私有错误码
（`-32001 TaskNotFound`、`-32002 TaskNotCancelable` 等）。

本系统使用自定义 HTTP 接口（`POST /runs` + SSE + `DELETE /runs/{id}`），
**语义上覆盖了同样四件事**（发起、流式、查询、取消），但未采用 JSON-RPC 封装。

未采用的理由：JSON-RPC 封装增加的是消息封装、错误码映射与方法路由——
属于**协议一致性**工作，不增加能力覆盖面。本项目的验证目标是发现机制的设计，
而非逐条实现规范。

### Webhook 推送与 `tasks/resubscribe`

异步推送解决的是「任务运行数小时、客户端不愿保持长连接」的问题。
本系统的 run 为秒级完成，SSE 已足够。断线续传已通过 `seq` 实现
（见 `app/api.py` 的 `from_seq`），采用重连补发而非推送。

### MCP client

MCP 是工具层的事实标准。未实现的理由：它是一个独立方向（工具发现与调用），
与自研工具层是两条路径。若要实现，应基于官方 `mcp` SDK 做适配层，
而非手写协议——MCP 规范迭代较快（2026-07-28 版本刚移除协议级 session），
手写实现无法持续跟进。

## 后续实现路径

按依赖顺序，每步可独立验证：

1. **JSON-RPC 薄壳**（约 1 天）：将现有 `/runs` 端点按 A2A 方法名重新暴露，
   补充错误码映射。完成后卡片中声明的 `preferredTransport: "JSONRPC"`
   即有对应实现。
2. **签名卡片**（约 2 天）：Ed25519 密钥对、JCS 规范化、`signatures[]` 字段、
   `/.well-known/agent.json/keys` 的 JWKS 端点，以及一个验签客户端。
3. **MCP 适配层**（约 2 天）：基于官方 SDK 将 MCP server 暴露的工具转换为
   `ToolSpec` 注入注册表。该步收益最大，可直接接入 MCP 生态。

## 参考

- [A2A 规范](https://github.com/a2aproject/A2A/blob/main/docs/specification.md)
- [Google Cloud 捐赠公告](https://developers.googleblog.com/en/google-cloud-donates-a2a-to-linux-foundation/)
- [MCP 规范](https://modelcontextprotocol.io/specification/)
- [Microsoft：多 agent 模式中 MCP 与 A2A 的分工](https://learn.microsoft.com/agents/architecture/multi-agent-patterns)
