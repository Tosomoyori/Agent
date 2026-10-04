"""内置工具集合。"""

from __future__ import annotations

from ..registry import ToolRegistry
from .fs import register_fs_tools
from .search import register_search_tools
from .shell import register_shell_tools

__all__ = ["register_builtin_tools", "default_registry", "BUILTIN_GROUPS"]

#: 工具分组，便于按需启用（比如只读场景可以不要 shell）。
BUILTIN_GROUPS = {
    "fs": register_fs_tools,
    "search": register_search_tools,
    "shell": register_shell_tools,
}


def register_builtin_tools(
    registry: ToolRegistry | None = None,
    *,
    groups: list[str] | None = None,
) -> ToolRegistry:
    """把内置工具注册进注册表，返回该注册表。

    :param groups: 只注册指定的分组。``None`` 表示全部。
    """
    registry = registry if registry is not None else ToolRegistry()
    selected = groups if groups is not None else list(BUILTIN_GROUPS)

    for name in selected:
        registerer = BUILTIN_GROUPS.get(name)
        if registerer is None:
            raise ValueError(
                f"未知的工具分组: {name!r}。可用: {', '.join(BUILTIN_GROUPS)}"
            )
        registerer(registry)

    return registry


def default_registry() -> ToolRegistry:
    """全部内置工具的新注册表。"""
    return register_builtin_tools()
