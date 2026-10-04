"""应用层：CLI 与 HTTP 服务。

这一层只依赖 ``agentkit.runtime``，不碰 ``llm`` / ``tools`` 的内部实现——
换一个前端（CLI / HTTP / 评测）不需要改动引擎。
"""

from __future__ import annotations

__all__ = ["main"]


def __getattr__(name: str):
    """惰性导出 CLI 入口，避免 ``import agentkit.app`` 时就把 argparse 也拖进来。"""
    if name == "main":
        from .cli import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
