---
kind: error_handling
name: AgentKit 错误分类与重试策略
category: error_handling
scope:
    - '**'
source_files:
    - src/agentkit/core/errors.py
    - src/agentkit/llm/retry.py
    - src/agentkit/app/cli.py
---

## 1. 总体方案

AgentKit 采用**自定义异常类层次 + 属性标记 + 集中式重试层**的方式处理错误，没有引入第三方异常库（如 `pydantic` 的 `ValidationError`、`httpx` 的 HTTPError 等）。所有业务异常都继承自 `AgentKitError`，并通过一个布尔属性 `retryable` 声明该错误是否值得重试；重试逻辑只认这个标记，不做类型名猜测。

## 2. 核心文件与层次

- `src/agentkit/core/errors.py`：定义全部错误类型与 `is_retryable()` 判定函数。
- `src/agentkit/llm/retry.py`：实现 `RetryPolicy`、`with_retry`、`stream_with_retry`，是 LLM 调用的唯一重试入口。
- `src/agentkit/app/cli.py`：CLI 顶层捕获 `AgentKitError`，统一输出到 stderr 并返回非零退出码。
- 其他模块（`core/config.py`、`discovery/registry.py`、`evaluation/runner.py`）抛出各自的异常，但上层通过 `AgentKitError` 做兜底处理。

## 3. 异常分类体系

```text
AgentKitError (基类)
├── ConfigurationError          # 配置缺失 / 非法
├── LLMError
│   ├── RateLimitError          # 429，retryable=True
│   ├── TransientLLMError       # 5xx / 网络抖动 / 超时，retryable=True
│   ├── AuthenticationError     # 401/403，不可重试
│   ├── InvalidRequestError     # 400，不可重试
│   │   └── ContextLengthExceeded  # 上下文超限，治法是裁剪历史
│   └── ModelOutputError        # 模型返回结构不可用
├── ToolError
│   ├── ToolNotFound
│   ├── ToolValidationError     # 参数校验失败 → 回灌给模型自我修正
│   ├── ToolExecutionError
│   └── PolicyError             # 安全策略拦截
│       ├── PathViolation
│       ├── CommandDenied
│       └── ApprovalDenied
├── MaxStepsExceeded            # 达到最大步数
├── BudgetExceeded              # 超出 token/成本/时长预算
├── RunCancelled                # run 被取消
└── InvalidConversation         # 消息序列违反不变量
```

每个子类在 `errors.py` 中通过类属性覆盖 `retryable: bool = False` 来声明可重试性，其中仅 `RateLimitError` 和 `TransientLLMError` 设为 `True`。`LLMError` 还携带可选的 `retry_after: float | None`，由 provider 适配器从 `Retry-After` / `retry-after-ms` 响应头映射填充，供重试层直接读取而无需解析原始响应。

## 4. 重试策略与约束

`RetryPolicy` 数据类封装重试参数：

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `max_attempts` | 3 | 总尝试次数（含首次），`__post_init__` 强制 ≥ 1 |
| `base_delay` | 0.5 | 退避基准秒数 |
| `max_delay` | 30.0 | 单次等待上限 |
| `respect_retry_after` | True | 是否尊重服务端 Retry-After |

`retry_delay()` 使用 **full jitter** 指数退避：`random.uniform(0, min(cap, base * 2**(attempt-1)))`，避免多个客户端同时限流后同步重试形成二次冲击。若服务端提供了 `retry_after`，则取 `max(0, min(retry_after, MAX_HONORED_RETRY_AFTER=60))`，防止服务端离谱的值挂住调用方。

`with_retry()` 与 `stream_with_retry()` 的关键行为：

1. **只重试显式标记的错误**：`except AgentKitError as exc; if not is_retryable(exc)` 即原样抛出，不猜类型名。
2. **关闭 SDK 自带重试**：文档明确要求构造 `AsyncOpenAI` 时传 `max_retries=0`，否则两层重试相乘会耗尽配额。
3. **流式请求只在第一个 chunk 之前可重试**：一旦 `emitted = True`，后续任何 `AgentKitError` 直接上抛——因为已经吐出的内容无法撤回，重试会造成重复输出与重复计费。
4. `on_retry(exc, attempt, delay)` 回调用于上层记录日志或发事件。

## 5. CLI 与边界展示

CLI 顶层 `try/except AgentKitError` 将运行时错误统一格式化为 `✗ <ClassName>: <message>` 输出到 stderr，并返回退出码 2；`KeyboardInterrupt` 返回 130。HTTP 服务 (`serve`) 本身不捕获业务异常，交由 uvicorn 默认处理。

## 6. 观察到的约定与约束

- **约定**：所有业务异常继承 `AgentKitError`，框架内部通过捕获 `AgentKitError` 做统一处理（CLI、重试层、以及各模块对 `AgentKitError` 的 `except` 块）。
- **约定**：可重试的错误子类必须把 `retryable = True` 写在类体上，并由注释说明原因（如 RateLimitError、TransientLLMError）。
- **约定**：provider 适配器在映射第三方异常时，应设置 `exc.retry_after` 以便重试层直接使用，而不是让重试层去解析原始响应。
- **约定**：工具参数校验失败走 `ToolValidationError`，语义上是“回灌给模型让它自我修正”，不是中断整个 run。
- **约束**：`RetryPolicy.max_attempts` 在 `__post_init__` 中强制 ≥ 1，否则抛出 `ValueError`。
- **约束**：重试层只捕获 `AgentKitError`，非此类型的异常（包括标准库异常、第三方异常）会直接冒泡，不会进入重试逻辑。
- **约束**：流式重试禁止在已产出元素后重试，这是正确性要求而非性能优化——文档明确说“调用方已经看到的东西没法撤回”。

## 7. 未覆盖之处

仓库中没有发现统一的错误码枚举、结构化错误响应 schema、或全局中间件式的错误处理器；HTTP API 层的错误呈现依赖 FastAPI/uvicorn 默认行为，未在代码中看到专门的异常到 HTTP 状态码的映射。