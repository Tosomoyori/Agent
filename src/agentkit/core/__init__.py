"""零依赖核心层：消息模型、事件、错误、配置、用量。

这一层不 import 任何其他 agentkit 子包，也不 import 任何第三方库
（pydantic 除外，它是数据模型的载体）。
"""

from . import errors, events, types, usage
from .config import Settings, load_settings
from .errors import (
    AgentKitError,
    ApprovalDenied,
    AuthenticationError,
    BudgetExceeded,
    CommandDenied,
    ConfigurationError,
    ContextLengthExceeded,
    InvalidConversation,
    InvalidRequestError,
    LLMError,
    MaxStepsExceeded,
    ModelOutputError,
    PathViolation,
    PolicyError,
    RateLimitError,
    RunCancelled,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolValidationError,
    TransientLLMError,
    is_retryable,
)
from .events import (
    TERMINAL_EVENT_TYPES,
    ApprovalRequested,
    ApprovalResolved,
    ReasoningDelta,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    StepStarted,
    TextDelta,
    ToolCallStarted,
    ToolResult,
    UsageReported,
)
from .events import (
    # 错误层和事件层各有一个 RunCancelled，语义不同（一个是异常，一个是终点事件）。
    # 这里把事件侧重命名，避免调用方混淆。
    RunCancelled as RunCancelledEvent,
)
from .types import (
    ContentBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    validate_conversation,
)
from .usage import ZERO_USAGE, Pricing, Usage, estimate_cost, pricing_for

__all__ = [
    "errors",
    "events",
    "types",
    "usage",
    "Settings",
    "load_settings",
    # errors
    "AgentKitError",
    "ApprovalDenied",
    "AuthenticationError",
    "BudgetExceeded",
    "CommandDenied",
    "ConfigurationError",
    "ContextLengthExceeded",
    "InvalidConversation",
    "InvalidRequestError",
    "LLMError",
    "MaxStepsExceeded",
    "ModelOutputError",
    "PathViolation",
    "PolicyError",
    "RateLimitError",
    "RunCancelled",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFound",
    "ToolValidationError",
    "TransientLLMError",
    "is_retryable",
    # events
    "TERMINAL_EVENT_TYPES",
    "ApprovalRequested",
    "ApprovalResolved",
    "ReasoningDelta",
    "RunCancelledEvent",
    "RunCompleted",
    "RunEvent",
    "RunFailed",
    "RunStarted",
    "StepStarted",
    "TextDelta",
    "ToolCallStarted",
    "ToolResult",
    "UsageReported",
    # types
    "ContentBlock",
    "Message",
    "ReasoningBlock",
    "Role",
    "TextBlock",
    "ToolResultBlock",
    "ToolSchema",
    "ToolUseBlock",
    "validate_conversation",
    # usage
    "ZERO_USAGE",
    "Pricing",
    "Usage",
    "estimate_cost",
    "pricing_for",
]
