"""记忆：上下文装配与持久化。

本模块只实现 **working memory + SQLite 持久化**。语义记忆、情节记忆、记忆整合
（consolidation）刻意没做，设计取舍见 ``docs/memory-design.md``。
"""

from .manager import MemoryManager
from .store import SessionInfo, SessionStore
from .working import (
    ContextWindow,
    WorkingMemory,
    estimate_messages_tokens,
    estimate_tokens,
)

__all__ = [
    "ContextWindow",
    "MemoryManager",
    "SessionInfo",
    "SessionStore",
    "WorkingMemory",
    "estimate_messages_tokens",
    "estimate_tokens",
]
