"""LLM Provider 抽象与适配器。"""

from ._json import (
    is_complete_json_object,
    join_argument_fragments,
    parse_tool_arguments,
)
from .base import (
    ChatModel,
    Completion,
    ModelCapabilities,
    StreamAccumulator,
    StreamChunk,
    ToolCallDelta,
    accumulate,
)
from .catalog import (
    FALLBACK_CAPABILITIES,
    KNOWN_MODELS,
    capabilities_for,
    is_known_model,
)
from .openai_compat import (
    OpenAICompatModel,
    extract_retry_after,
    map_openai_error,
    normalize_usage,
    to_openai_messages,
    to_openai_tools,
    to_stream_chunk,
)
from .retry import RetryPolicy, retry_delay, stream_with_retry, with_retry

__all__ = [
    "FALLBACK_CAPABILITIES",
    "KNOWN_MODELS",
    "ChatModel",
    "Completion",
    "ModelCapabilities",
    "OpenAICompatModel",
    "RetryPolicy",
    "StreamAccumulator",
    "StreamChunk",
    "ToolCallDelta",
    "accumulate",
    "capabilities_for",
    "extract_retry_after",
    "is_complete_json_object",
    "is_known_model",
    "join_argument_fragments",
    "map_openai_error",
    "normalize_usage",
    "parse_tool_arguments",
    "retry_delay",
    "stream_with_retry",
    "to_openai_messages",
    "to_openai_tools",
    "to_stream_chunk",
    "with_retry",
]
