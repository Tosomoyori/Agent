"""AgentKit —— 从零实现的 Agent 开发框架。

零依赖层在 :mod:`agentkit.core`，其余按依赖方向单向排列::

    core  ←  llm / tools / observability  ←  memory  ←  runtime  ←  app

公开 API::

    from agentkit import Agent, ToolRegistry, tool, Settings
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Agent",
    "ToolRegistry",
    "tool",
    "Settings",
    "ChatModel",
]


def __getattr__(name: str):  # pragma: no cover - 简单的惰性导入
    """惰性导出。让 ``import agentkit`` 保持轻量，不牵连 fastapi 之类的重依赖。"""
    if name == "Agent":
        from .runtime.agent import Agent

        return Agent
    if name in ("ToolRegistry", "tool"):
        from . import tools

        return getattr(tools, name)
    if name == "Settings":
        from .core.config import Settings

        return Settings
    if name == "ChatModel":
        from .llm.base import ChatModel

        return ChatModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
