# AgentKit

从零实现的 Agent 开发框架。核心循环、工具层、记忆、可观测性、评测全部自研，
另附 LangChain 互操作层，支持双向接入生态。

- 源码 8,700 余行，测试 4,700 余行，离线单测 409 个
- 目标模型：DeepSeek `deepseek-flash`（OpenAI 兼容接口，可切换其他 provider）
- 交付形态：库 + CLI + HTTP 服务（SSE）+ Web 控制台

## 架构

```
src/agentkit/
├── core/           零依赖层：内容块消息模型、事件、错误分类、配置、用量记账
├── llm/            Provider 抽象、流式增量归一化、重试策略、模型能力目录
├── tools/          工具协议与 schema 生成、注册表、安全策略、内置工具
├── memory/         工作记忆（预算裁剪 + 摘要压缩）与 SQLite 持久化
├── runtime/        引擎循环、事件总线、预算、取消传播、审批通道
├── observability/  span 树与导出（对齐 OTel GenAI 语义约定）
├── discovery/      AgentCard 注册与发现（对齐 A2A 协议）
├── evaluation/     评测数据集、判据、指标、运行器、报告
├── integrations/   生态互操作层（可选依赖）
└── app/            CLI、HTTP 服务（SSE）、Web 控制台
```

依赖方向单向：`core` 零依赖；`llm` / `tools` / `observability` 只依赖 `core`；
`runtime` 依赖全部；`app` 只依赖 `runtime`。**`tools` 不 import `runtime`**——
审批接口在 `tools` 中定义为协议，由 `runtime` 注入实现。
互操作层只依赖 `core` / `llm` / `tools`，不涉及 `runtime`。

## 实测结果

### 评测

`uv run agentkit eval eval_suites --mode live --trials 5`，22 个用例 × 5 次试验
= 110 次真实调用：

| 指标 | 值 |
| --- | --- |
| 任务成功率 (TSR) | 100.0% |
| pass@5 / pass^5 | 100.0% / 100.0% |
| 工具调用准确率 | 100.0% |
| 平均步数 | 2.95 |
| 延迟 P50 / P95 | 2932 ms / 4677 ms |
| 每次成功成本 | 0.0008 CNY |

该结果说明基线稳定、零失败；**不代表 agent 能力已饱和**。22 个用例考察的是
工具选择、递归遍历、干扰项排除、条件分支、跨文件比对等能力，这些在 2026 年的
前沿模型上已接近上限，因此该套件对当前模型不具区分度。作为回归基线有效，
作为能力证明不成立。完整的指标定义、结果解读与局限见
[docs/evaluation.md](docs/evaluation.md)。

### 性能

`scripts/loadtest.py`，并发 1 / 5 / 10 各 8 次，共 24 次真实调用，零失败：

| 并发 | 吞吐 | 总延迟 P50 / P95 | 引擎耗时 P50 |
| ---: | ---: | ---: | ---: |
| 1 | 0.52 /s | 1954 / 2223 ms | 1945 ms |
| 5 | 1.70 /s | 1969 / 2532 ms | 1945 ms |
| 10 | 3.58 /s | 1818 / 2182 ms | 1780 ms |

延迟不随并发上升（P50 稳定在 1.9 秒），说明框架内部无排队或争用。
「引擎耗时」与「总延迟」相差约 9 ms，即框架自身开销低于 1%，
其余时间用于等待模型响应。**总延迟不可作为框架性能指标引用。**

未覆盖的场景（更高并发、长时间运行、多轮会话、子进程工具、多实例部署）
见 [docs/performance.md](docs/performance.md)。

## 关键设计决策

完整记录（问题 / 选项 / 决定 / 代价）见 [docs/adr/](docs/adr/)。

| 决策 | 依据 |
| --- | --- |
| 内部消息用**内容块模型**，而非 OpenAI 的 `tool_calls[]` + `role:"tool"` | Anthropic 无 `tool` role，其 `tool_use`/`tool_result` 是消息内的 content block 且要求严格相邻配对。内部规范成 block 后转 Anthropic 是直接映射；反之每次都需要重新分组重排 |
| **原生 function calling** 取代文本解析式 ReAct | 文本解析的格式错误率业界约 17–23%，且无法表达并行工具调用 |
| 引擎只产出**类型化事件** | SSE、CLI、可观测、评测四个消费者共享同一数据流，避免「返回值 + 回调」形成两个真相源 |
| 引擎事件经**队列**而非直接产出 | 审批事件必须在工具仍阻塞时送达客户端，否则退化为「先出结果再询问是否批准」 |
| 工具参数经 **pydantic 校验，失败回灌给模型** | 参数错误属于模型可自我修正的失败，转成可读的错误结果优于中断整个 run |
| 路径边界用 **`realpath` + `commonpath`** | 字符串前缀比较不解析符号链接，工作区内一个 symlink 即可越界；`C:\foo` 也不是 `C:\foobar` 的父目录 |
| 命令安全用**三分裁决**（拒绝 / 放行 / 转审批） | 正则黑名单必然遗漏（`rm -r -f`、`rm${IFS}-rf`）且会误杀（`echo "rm -rf /"` 仅为字符串） |
| 流式重试**仅在首个 chunk 之前**允许 | 已发送给调用方的内容不可撤回，重试将导致重复输出且该部分输入 token 已计费 |
| 可观测性是事件流的**消费者**，不在引擎内埋点 | 成对埋点迟早遗漏一处（返回值未接住、异常分支未关闭），trace 中会留下永不结束的 span |
| 记忆**只做工作记忆 + 持久化** | 语义记忆与情节记忆需要嵌入模型和向量库，且缺乏可接受的评测方式。取舍见 [docs/memory-design.md](docs/memory-design.md) |
| A2A 只做**卡片 + 注册发现** | 签名卡片与 JSON-RPC 服务端属于协议一致性工作，不增加能力覆盖面。取舍见 [docs/a2a-scope.md](docs/a2a-scope.md) |
| 评测判据**优先使用正面事实** | 负面字符串判据检查的是措辞而非事实，会惩罚正确回答 |
| 评测分 **hermetic / live 两层** | hermetic 使用回放模型验证评测框架自身，可进 CI 且零成本；agent 真实能力只能在 live 模式测量 |

## 使用

需要 [uv](https://docs.uv.io/)，Python 版本由 `.python-version` 固定。

```bash
uv sync --extra dev          # 建 .venv 并装依赖
uv sync --extra langchain    # 可选：需要 LangChain 互操作时
cp .env.example .env         # 填入 DEEPSEEK_API_KEY
```

### CLI

```bash
uv run agentkit run "列出当前目录的文件，并说明 README.md 有多少行"
uv run agentkit run "读一下 README" --session s1   # 指定会话，跨轮次保留上下文
uv run agentkit chat                                # 交互式对话
uv run agentkit sessions list                       # 查看历史会话
uv run agentkit tools                               # 列出工具及其参数 schema
```

需要审批的动作（写文件、执行未知命令）在终端中提示；被拒绝时模型收到一条明确的
错误结果，并将该步骤交回给用户，而非反复重试。非交互场景使用 `--no-approve`
直接拒绝所有需审批动作——默认拒绝，非默认放行。

### HTTP 服务

```bash
uv run agentkit serve
```

| 端点 | 说明 |
| --- | --- |
| `http://127.0.0.1:8000/` | Web 控制台：推理链路、工具调用、审批、trace 树 |
| `/.well-known/agent-card.json` | A2A 约定的 AgentCard |
| `/agents` | 已注册 agent 目录，支持按标签过滤 |
| `/healthz` | 健康检查 |

trace 写入 `.agentkit/traces.jsonl`。

### 评测

```bash
uv run agentkit eval eval_suites --mode hermetic    # 框架自检，零成本
uv run agentkit eval eval_suites --mode live -n 5   # 真实评测，产生 API 费用
```

结果以 Markdown 与 JSON 输出到 `eval_reports/`。

### LangChain 互操作

```python
from agentkit.integrations.langchain import AgentKitChatModel, to_langchain_tools

llm = AgentKitChatModel(model=build_model(load_settings()))
tools = to_langchain_tools(registry)
reply = await llm.bind_tools(tools).ainvoke([HumanMessage("...")])
```

可运行示例见 [examples/langchain_interop.py](examples/langchain_interop.py)。

## 开发

```bash
uv run pytest                # 离线单测，使用回放模型，不访问网络
uv run pytest -m live        # 真实调用模型的冒烟测试，产生费用
uv run ruff check .          # 静态检查
```

### 测试策略

模型输出具有不确定性，Agent 的行为则不必如此。方法是将不确定性收敛到
`ChatModel` 这一个边界：测试中注入按脚本返回预设响应的回放模型
（`tests/conftest.py` 的 `ScriptedModel`），引擎循环、工具执行、消息转换、
事件序列随即成为确定性逻辑，无需网络、不产生费用、结果可复现。

真实模型只在两处出现：`-m live` 的冒烟测试，以及录制 provider 协议行为的
fixture（见 [docs/provider-notes.md](docs/provider-notes.md)）。

## 能力边界

以下为**已知缺口**，均已在对应文档中记录成因与后续路径：

| 缺口 | 说明 |
| --- | --- |
| run 级中断恢复 | 目前仅有会话级持久化、事件流可观测、以及落盘前的配对不变量校验。完整的崩溃恢复需要 append-only 事件日志、重放与副作用幂等键。见 [ADR 0005](docs/adr/0005-session-persistence.md) |
| 副作用幂等键 | `ToolSpec.idempotent` 标记已就位，幂等键机制未实现 |
| 命令沙箱 | 允许清单是能力限制器，不是沙箱。`python -c` 等同于任意代码执行，真正的隔离需要容器。该限制已固定为测试。见 [ADR 0006](docs/adr/0006-command-policy.md) |
| 评测区分度 | 22 个用例对当前模型偏容易，不具区分度。见 [docs/evaluation.md](docs/evaluation.md) |
| 长时间运行 | 未验证数小时运行后的内存增长、SQLite 膨胀与连接泄漏。见 [docs/performance.md](docs/performance.md) |
| MCP client | 未实现。MCP 规范迭代较快，手写协议不可持续，应基于官方 SDK 做适配 |

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/adr/](docs/adr/) | 9 份架构决策记录，含决策代价 |
| [docs/provider-notes.md](docs/provider-notes.md) | 实测得到的 provider 流式行为 |
| [docs/evaluation.md](docs/evaluation.md) | 评测设置、结果、判据设计约束、局限 |
| [docs/performance.md](docs/performance.md) | 压测数据与未覆盖场景 |
| [docs/memory-design.md](docs/memory-design.md) | 记忆系统的实现范围与取舍 |
| [docs/a2a-scope.md](docs/a2a-scope.md) | Agent 发现能力的实现范围与取舍 |
| [scripts/](scripts/) | provider 流式抓取、压测 |
