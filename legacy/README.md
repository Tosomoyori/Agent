# legacy —— 改造前的原始实现

这里是重构前的单文件 ReAct agent（`agent.py` + `prompt_template.py`），保留下来作为对照，**不参与构建，也不被 `src/agentkit` 引用**。

保留它的理由：面试时「为什么要重写」这个问题，指着代码讲比空口说要清楚得多。下面列出的是这次重写针对的具体问题。

## 被替换掉的设计

| 位置 | 问题 |
| --- | --- |
| `agent.py:385` `_parse_llm_response` | 用正则从模型输出里抠 JSON，解析失败就退化成「原始输出」塞回下一轮。业界文本式 ReAct 的格式错误率在 17–23%。新实现改用**原生 function calling**，参数由 API 保证是结构化 JSON。 |
| `agent.py:245` | `ReActSession.to_messages` 把工具结果伪装成 `role: "user"` 的「观察结果：…」文本。这既污染了对话语义，也无法表达并行工具调用。新实现用 **content-block 消息模型**。 |
| `agent.py:83` `_ensure_safe_path` | 用 `os.path.abspath` + `startswith` 判断路径边界。**不解析符号链接**，工作区内一个指向外部的 symlink 就能绕过；`C:\foo` 与 `C:\foobar` 的前缀比较也不严谨。新实现用 `realpath` + `commonpath`。 |
| `agent.py:24` `DANGEROUS_PATTERNS` | 正则黑名单拦危险命令，必然漏（`rm -r -f`、`rm${IFS}-rf`、`python -c "..."`、PowerShell 等价写法）。新实现换成**白名单策略 + 高危动作转审批**。 |
| `agent.py:189` | 审批走 `input()` 阻塞式读 stdin。这在 CLI 里勉强能用，但服务化（SSE）之后根本没有 stdin 可读。新实现把审批建模成**异步事件**。 |
| `agent.py:35` `WORKSPACE` | 模块级全局变量存工作区路径，工具函数靠它做边界检查。进程内只能有一个工作区，也没法并发跑多个 run。新实现把它收进 `ToolContext` 显式传递。 |
| `agent.py:263` | 硬编码默认模型 `deepseek-chat`。该模型已于 2026 年下线，`GET /models` 现在只返回 `deepseek-flash` 和 `deepseek-v4-pro`。新实现把模型名提为配置项。 |
| `agent.py:338` | `tool_func(**params)` 直接调用，异常统一转成字符串返回。无法区分「模型传错了参数」和「工具内部出错」，前者本可以让模型自我修正。新实现用 pydantic 校验参数，校验失败回灌成 `is_error` 结果。 |

## 仍然有价值的部分

- `ToolRegistry` 的装饰器注册思路被继承，扩展成了带 JSON Schema 自动生成和参数校验的版本。
- 五个内置工具（读/写/列目录/搜索/执行命令）逐一移植，行为语义保持一致。

## 运行方式

旧实现依赖 `openai` 和 `python-dotenv`，模型名需要手动改成 `deepseek-flash` 才能跑：

```bash
python legacy/agent.py <工作目录> -q "你的问题"
```
