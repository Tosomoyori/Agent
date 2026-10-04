"""事件总线。

**为什么引擎不直接 yield 事件？**

因为审批。SSE 场景下客户端必须**在工具还阻塞等待的时候**就收到审批请求——
它要看到请求才能做出决定。如果事件只能在 ``await`` 返回之后才 yield 出去，
审批就变成了「先等出结果，再问你要不要批准」，逻辑上倒过来了。

用队列把生产者和消费者解耦之后，任何深度的代码路径（引擎循环、注册表、
工具实现、审批通道）都能立即推事件，消费者按自己的节奏取。

顺带解决的另一件事：**异常不会让消费者挂住**。生产者无论以何种方式结束，
``finally`` 里都会关掉总线；消费者因此总能等到一个终止事件或流的正常结束。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator

from ..core.events import RunEvent

__all__ = ["EventBus"]

#: 队列里的哨兵，表示「不会再有事件了」。
_CLOSED = None


class EventBus:
    """单次 run 内的事件队列。"""

    def __init__(self, run_id: str, *, maxsize: int = 0) -> None:
        self.run_id = run_id
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=maxsize)
        self._seq = itertools.count(1)
        self._closed = False

    def emit(self, event: RunEvent) -> RunEvent:
        """同步入队，顺带盖上 ``seq`` 和 ``run_id``。

        刻意做成同步的：调用它的时候往往正在处理别的事情（比如工具执行到一半
        要发审批请求），再去 ``await`` 一个入队操作只会白白增加复杂度。
        """
        if self._closed:
            # 关掉之后再发的事件丢掉即可。常见于取消之后某个工具才慢悠悠地
            # 报错——那条信息已经没有消费者了。
            return event

        event.seq = next(self._seq)
        event.run_id = self.run_id
        self._queue.put_nowait(event)
        return event

    async def drain(self) -> AsyncIterator[RunEvent]:
        """消费到终止事件或总线关闭为止。"""
        while True:
            event = await self._queue.get()
            if event is _CLOSED:
                return
            yield event

    def close(self) -> None:
        """宣告结束。幂等。"""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(_CLOSED)

    @property
    def closed(self) -> bool:
        return self._closed
