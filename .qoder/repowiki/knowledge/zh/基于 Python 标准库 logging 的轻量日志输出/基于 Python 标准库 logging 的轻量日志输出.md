---
kind: logging_system
name: 基于 Python 标准库 logging 的轻量日志输出
category: logging_system
scope:
    - '**'
source_files:
    - src/agentkit/app/api.py
    - src/agentkit/runtime/engine.py
---

## 1. 使用的系统/方案

项目没有引入第三方日志框架（如 `loguru`、`structlog`、`python-json-logger`），仅使用 Python 标准库 `logging`。所有日志通过模块级 `logger = logging.getLogger(__name__)` 获取，调用点集中在两个文件：

- `src/agentkit/app/api.py` — HTTP 服务层，记录 run 失败等异常路径。
- `src/agentkit/runtime/engine.py` — 推理引擎，记录未预期异常、会话落盘失败等运行时状态。

没有在代码中调用 `logging.basicConfig` / `setLevel` / `addHandler`，因此日志级别和输出目标由**应用启动方**（FastAPI 进程或测试）负责配置；包本身不强制任何 handler 或 formatter。

## 2. 关键文件

- `src/agentkit/app/api.py`：定义 `logger = logging.getLogger(__name__)`，在 `_pump` 的异常分支中使用 `logger.exception("run %s 失败", state.run_id)`。
- `src/agentkit/runtime/engine.py`：定义 `logger = logging.getLogger(__name__)`，两处使用——`_persist` 中用 `logger.info(...)` 记录跳过落盘的会话，以及 `_drive` 的兜底 `except Exception` 分支用 `logger.exception("run %s 出现未预期的异常", bus.run_id)`。

其余模块（`core/`、`llm/`、`tools/`、`memory/`、`discovery/`、`evaluation/`、`observability/`）均未直接使用 `logging`。

## 3. 架构与约定

- **按模块命名空间隔离 logger**：每个使用日志的模块通过 `logging.getLogger(__name__)` 创建独立 logger，便于外部按包名过滤（例如只打开 `agentkit.runtime.engine` 的日志）。
- **结构化字段走事件对象而非日志字段**：运行期上下文（`run_id`、`session_id`、`tool_use_id`、`step` 等）通过 `RunEvent` 数据类在事件总线上传播，而不是作为日志记录的键值对。日志只承载人类可读的诊断信息，真正的可观测性由 `observability.Observer` + `JsonlExporter` 导出到 `traces.jsonl` 承担（见 `app/api.py` 中 `RunManager._make_observer`）。
- **日志级别选择**：`engine.py` 中“会话消息配对不完整”走 `logger.info`（属于预期内的降级路径），异常堆栈走 `logger.exception`（自动附带 traceback）。`api.py` 中 run 失败也走 `logger.exception`。
- **无全局初始化**：包内没有任何地方设置 root logger 的 level/handler/formatter，也没有自定义 Formatter/Filter/Handler。这意味着部署者必须自行调用 `basicConfig` 或通过 FastAPI 中间件配置日志。

## 4. 约定与约束

- **观察到的约定**：
  - 需要日志的模块统一以 `import logging` + `logger = logging.getLogger(__name__)` 开头。
  - 日志消息使用 f-string 风格的位置参数（如 `"run %s 失败"`、`"run %s 出现未预期的异常"`、`"会话 %s 的消息落盘失败"`），避免字符串拼接。
  - 日志不包含业务 payload（prompt、工具参数、模型输出），这些内容通过 `RunEvent` 和 trace exporter 输出，以避免把大文本塞进 stdout/stderr。
- **无硬性规则**：仓库中没有 lint 规则、文档或类型注解强制要求使用 `logging`；也没有统一的 log level 策略文档。上述行为是现有代码中的实际模式，不是被框架强制的约束。
- **可观测性与日志分离**：`observability/exporters.py` 中的 `JsonlExporter` 负责把结构化 trace 写入文件，而 `logging` 仅用于进程级诊断输出，二者职责明确分开。