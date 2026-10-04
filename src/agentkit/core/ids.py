"""标识符生成。

统一在一处生成，好处是测试里可以整体打桩——需要断言 run_id / tool_call_id 的地方
不必去猜随机值，评测重放时也能保证 id 稳定。
"""

from __future__ import annotations

import uuid

__all__ = ["new_id", "new_run_id", "new_call_id"]


def new_id(prefix: str) -> str:
    """生成 ``<prefix>_<24 位十六进制>`` 形式的 id。"""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def new_run_id() -> str:
    return new_id("run")


def new_call_id() -> str:
    return new_id("call")
