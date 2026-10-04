# A2A（Agent 注册与发现）：做到哪，为什么停在这

JD 里有一条「参与 Agent 注册、管理、发现能力的架构设计和系统开发」。这份文档
说明本项目在这条上实现了什么、哪些是刻意没做的、以及没做的理由。

---

## 背景：两条互补的协议

Agent 互操作在 2026 年已经收敛出两条事实标准：

| 协议 | 管什么 | 一句话 |
| --- | --- | --- |
| **MCP** (Model Context Protocol) | agent ↔ 工具/数据 | 垂直：一个 agent **内部**怎么用工具 |
| **A2A** (Agent2Agent) | agent ↔ agent | 水平：一个 agent 怎么发现并调用**另一个** agent |

A2A 由 Google 在 2025 年 4 月发起，同年 6 月捐给 Linux Foundation，2026 年 8 月
转入 Agentic AI Foundation（与 MCP 同属一个基金会，但各自保留 maintainer 和发布
节奏）。它的核心机制是三件事：

1. **Agent Card**——一个 agent 的自述文件，放在约定的
   `/.well-known/agent-card.json`；
2. **能力发现**——客户端拉卡片，解析 `skills[]` / `capabilities` / `securitySchemes`；
3. **任务交换**——JSON-RPC 2.0 over HTTP，流式走 SSE，异步走 webhook 推送。

第 2 条是关键：它把「去哪里问一个服务它能做什么」标准化成了一句约定的 URL。
没有这个约定，每个 agent 平台都得自己发明一套服务目录。

---

## 本项目实现了什么

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| AgentCard 数据模型 | [`discovery/card.py`](../src/agentkit/discovery/card.py) | 字段对齐 A2A，`to_well_known()` 负责 camelCase 转换 |
| 注册 / 管理 / 发现 | [`discovery/registry.py`](../src/agentkit/discovery/registry.py) | 注册、注销、按标签检索、歧义检测 |
| `/.well-known/agent-card.json` | [`app/api.py`](../src/agentkit/app/api.py) | 由 FastAPI 挂载，返回本服务的卡片 |
| `/agents` 目录接口 | 同上 | 列出注册表里的全部 agent，支持按标签过滤 |
| 技能由工具推导 | 同上 `_skills_for` | 卡片里的 `skills[]` 从**实际注册的工具**生成，不是手写清单 |

**一个刻意的设计：技能从工具推导。** 手写一份技能清单很容易，但它迟早和实现对不上——
某天有人加了个工具却忘了更新卡片，发现机制就会把一个它其实做不了的任务派过来。
从 `registry.names()` 推导虽然笨，但没有漂移的可能。

---

## 刻意没做的

### 1. 签名 Agent Card（JCS + Ed25519 detached JWS）

这是 A2A v1.0 的招牌特性：用 RFC 8785 的 JSON 规范化 + RFC 8037 的 Ed25519 签名，
保护卡片的**身份层**（不保护声明层）。为什么不做：

* 实现量不小（规范化、密钥管理、JWKS 端点、验签）；
* **在控制台 demo 里一点都展示不出来**——签名是否正确，看是看不出来的；
* 它解决的问题（跨组织的 agent 身份信任）在本项目「单机跑几个 agent」的
  场景里不存在。

面试被问到就答：「我知道 v1.0 用吊销式 JWS 保护身份层而不是声明层，也理解
为什么——声明可以随时改而身份必须稳定。评估后判断它对我的演示目标性价比太低。」

### 2. JSON-RPC 2.0 服务端与 `tasks/*` 方法族

A2A 的任务交换定义了完整的一套方法：`message/send`、`message/stream`、
`tasks/get`、`tasks/list`、`tasks/cancel`、`tasks/resubscribe`，以及一批私有错误码
（`-32001 TaskNotFound`、`-32002 TaskNotCancelable` 等）。

本项目用的是自己的 HTTP 接口（`POST /runs` + SSE + `DELETE /runs/{id}`），
**语义上覆盖了同样的四件事**（发起、流式、查询、取消），但没有套 JSON-RPC 的壳。

不做全套的理由：套上 JSON-RPC 之后，多出来的是消息封装、错误码映射、方法路由——
这些是**协议一致性**工作，不是**能力**工作。本项目要证明的是「我知道发现机制怎么
设计」，而不是「我能照着规范抄一遍」。

### 3. Webhook 推送与 `tasks/resubscribe`

异步推送解决的是「任务跑几小时，客户端不想一直挂着连接」。本项目的 run 秒级完成，
SSE 足够。断线续传已经用 `seq` 实现了（见 `app/api.py` 的 `from_seq`），
只是走的是重连补发而不是推送。

### 4. MCP client

`docs/memory-design.md` 里提过 MCP 是工具层的标准。本项目没实现 MCP client，
理由类似：它是一个**独立的方向**（工具发现与调用），和本项目的「自研工具层」
是两条路。真要做，应该用官方 `mcp` SDK 做适配层，而不是手写协议——
MCP 的 spec 更新很快（2026-07-28 版本刚移除了协议级 session），手写跟不上。

---

## 如果要做，路径是什么

按依赖排序，每步独立可验证：

1. **JSON-RPC 薄壳**（约 1 天）——把现有的 `/runs` 端点按 A2A 的方法名重新暴露，
   加上错误码映射。这一步做完，卡片里声明的 `preferredTransport: "JSONRPC"`
   就不再是空头支票。
2. **签名卡片**（约 2 天）——Ed25519 密钥对、JCS 规范化、`signatures[]` 字段、
   `/.well-known/agent.json/keys` 的 JWKS 端点，外加一个验签客户端。
3. **MCP 适配层**（约 2 天）——用官方 SDK 把 MCP server 暴露的工具转成
   `ToolSpec` 注入注册表。这一步的收益最大：立刻接上整个 MCP 生态。

---

## 参考

- [A2A 规范](https://github.com/a2aproject/A2A/blob/main/docs/specification.md)
- [Google Cloud 捐赠公告](https://developers.googleblog.com/en/google-cloud-donates-a2a-to-linux-foundation/)
- [MCP 规范](https://modelcontextprotocol.io/specification/)
- [Microsoft：多 agent 模式中 MCP 与 A2A 的分工](https://learn.microsoft.com/agents/architecture/multi-agent-patterns)
