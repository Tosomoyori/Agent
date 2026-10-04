"""错误分类学。

重试策略直接依赖这里的 ``retryable`` 标记：只有 429 / 5xx / 网络抖动值得重试，
400、401、403 重试只会继续失败还白烧钱。

区分错误的另一个用途是决定「回灌给模型」还是「中断整个 run」：
工具参数校验失败属于前者（模型看见自己的错误可以自我修正），
认证失败属于后者（再怎么重试都没用）。
"""

from __future__ import annotations


class AgentKitError(Exception):
    """所有 agentkit 错误的基类。"""

    #: 该错误是否值得重试。子类按需覆盖。
    retryable: bool = False


class ConfigurationError(AgentKitError):
    """配置缺失或非法，例如没有提供 API Key。"""


# ---------------------------------------------------------------- LLM


class LLMError(AgentKitError):
    """调用 LLM 失败。"""


class RateLimitError(LLMError):
    """429。DeepSeek 限的是并发数而非 RPM，超限时返回此错误。"""

    retryable = True


class TransientLLMError(LLMError):
    """5xx、网络抖动、超时——重试有意义。"""

    retryable = True


class AuthenticationError(LLMError):
    """401 / 403。重试只会继续失败。"""


class InvalidRequestError(LLMError):
    """400。请求本身有问题，重试无用。"""


class ContextLengthExceeded(InvalidRequestError):
    """输入超出模型上下文窗口。治法是裁剪历史，不是重试。"""


class ModelOutputError(LLMError):
    """模型返回了结构上无法使用的内容，例如 tool_call 的参数不是合法 JSON。"""


# ---------------------------------------------------------------- 工具


class ToolError(AgentKitError):
    """工具相关错误的基类。"""


class ToolNotFound(ToolError):
    """模型请求了一个不存在的工具。"""


class ToolValidationError(ToolError):
    """模型给的参数不符合工具的 schema。

    这类错误应当被回灌给模型让它自我修正，而不是中断 run。
    """


class ToolExecutionError(ToolError):
    """工具内部执行失败。"""


# ---------------------------------------------------------------- 策略 / 安全


class PolicyError(ToolError):
    """工具调用被安全策略拦截。"""


class PathViolation(PolicyError):
    """路径越出工作区边界。"""


class CommandDenied(PolicyError):
    """命令被策略拒绝。"""


class ApprovalDenied(PolicyError):
    """需要人工审批的动作被拒绝或审批超时。"""


# ---------------------------------------------------------------- 运行时


class MaxStepsExceeded(AgentKitError):
    """达到最大步数仍未给出最终答复。"""


class BudgetExceeded(AgentKitError):
    """超出 token / 成本 / 时长预算。"""


class RunCancelled(AgentKitError):
    """run 被取消。"""


class InvalidConversation(AgentKitError):
    """消息序列违反配对不变量，见 :func:`agentkit.core.types.validate_conversation`。"""


def is_retryable(exc: BaseException) -> bool:
    """该异常是否值得重试。

    只认显式标记，不做类型名猜测——猜错会把不可重试的错误变成重复扣费。
    """
    return bool(getattr(exc, "retryable", False))
