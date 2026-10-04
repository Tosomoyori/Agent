"""LLM Provider 抽象与适配器。"""

from .base import ChatModel, Completion, ModelCapabilities
from .openai_compat import (
    OpenAICompatModel,
    from_openai_message,
    map_openai_error,
    normalize_usage,
    parse_tool_arguments,
    to_openai_messages,
    to_openai_tools,
)

__all__ = [
    "ChatModel",
    "Completion",
    "ModelCapabilities",
    "OpenAICompatModel",
    "from_openai_message",
    "map_openai_error",
    "normalize_usage",
    "parse_tool_arguments",
    "to_openai_messages",
    "to_openai_tools",
]
