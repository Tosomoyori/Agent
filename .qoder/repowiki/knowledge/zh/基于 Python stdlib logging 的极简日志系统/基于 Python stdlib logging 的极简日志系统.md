---
kind: logging_system
name: 基于 Python stdlib logging 的极简日志系统
category: logging_system
scope:
    - '**'
source_files:
    - src/agentkit/runtime/engine.py
    - src/agentkit/app/cli.py
---

## 1. 使用的框架/方案

仓库没有引入第三方日志库（如 loguru、structlog、logzero），仅使用 Python 标准库 `logging`。全仓唯一一处 logger 定义位于 `src/agentkit/runtime/engine.py`：

```python
import logging
logger = logging.getLogger(__name__)
```

其余模块通过 `print()` / `sys.stderr` 直接输出 CLI 交互信息，不经过 logger。

## 2. 关键文件

- `src/agentkit/runtime/engine.py` — 仓库中唯一使用 `logging` 的文件，定义了 `logger = logging.getLogger(__name__)`，并在两处调用：
  - `logger.info(...)`：会话消息配对不完整时跳过落盘（第 485 行）。
  - `logger.exception(...)`：捕获未预期的内部异常并记录完整 traceback（第 240 行）。
- `src/agentkit/app/cli.py` — CLI 入口，所有面向用户的输出均通过 `print()` / `sys.stderr` 完成，无 logger 初始化或配置。

## 3. 架构与约定

- **单点 logger**：整个应用只有一个 logger 实例，按模块名 (`__name__`) 获取，遵循 stdlib 命名空间惯例。
- **日志级别**：只使用了 `info` 和 `exception` 两个级别；未见 `debug`/`warning`/`error` 的使用。
- **结构化字段**：无结构化日志（无 JSON 序列化、无固定字段 schema）。日志为纯文本中文消息，参数以 `%s` 风格格式化。
- **Sink/输出**：未对 root logger 做任何 handler/formatter/configure 设置，因此依赖 Python 默认行为（无输出，除非调用方显式配置）。CLI 层也没有在启动时调用 `logging.basicConfig`。
- **错误处理中的日志**：引擎在 `_drive` 顶层 `except Exception` 分支用 `logger.exception` 记录未预期异常，同时把摘要作为 `RunFailed` 事件推给调用方——注释明确说明“栈写进日志，事件里只放摘要，否则一条 traceback 会把 SSE 载荷撑得没法看”。
- **CLI 输出与日志分离**：用户可见的输出（run/chat 命令）全部走 `ConsoleRenderer` + `print`，与 `logging` 完全解耦；`--json-events` 模式则把 `RunEvent.model_dump_json()` 直接写入 stdout，也不经过 logger。

## 4. 观察到的约定与约束

- **约定（描述性）**：
  - 仅在运行时核心逻辑（engine）中使用 `logging`，业务/工具/LLM 层不直接打日志。
  - 日志消息使用中文，便于本地调试阅读。
  - CLI 的用户交互输出一律通过 `print`/`sys.stderr`，不在 logger 上输出。
  - 未预期的异常统一经 `logger.exception` 记录后再转为事件，避免 traceback 污染事件负载。
- **约束（由实现强制）**：
  - 仓库中没有 `logging.basicConfig`、`StreamHandler`、`FileHandler` 等任何 sink 注册代码，因此默认情况下不会向控制台/文件输出任何日志；要启用日志需外部调用方自行配置 root logger。
  - 由于只有 `engine.py` 一个 logger 被使用，不存在多模块 logger 命名空间冲突的风险，但这也意味着其他模块无法复用该 logger。

总体而言，这是一个极简的、几乎未配置的 stdlib `logging` 用法：仅一个 logger、两个级别、无结构化字段、无自定义 sink，主要用途是记录引擎层的意外异常和持久化失败。