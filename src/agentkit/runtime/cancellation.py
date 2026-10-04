"""run 级取消。

**为什么不能只靠 ``asyncio.CancelledError``：**

1. 取消要能**传进工具**。工具在子进程里跑（``run_command``），或者在做分块 IO，
   光取消外层 task 会让子进程变成孤儿，一直占着资源。
2. 取消要能**说明原因**。是用户点了取消按钮，还是 run 超时了？两者都该以
   ``run_cancelled`` 事件收尾，但原因不同，日志里要看得出来。
3. 取消是**正常终止**，不是异常。它必须能让引擎发完最后一个事件再退出，
   而不是把异常抛穿到调用方那里变成一个 500。

所以用一个显式的 token 在整条调用链上传递，而不是依赖 task 取消的隐式传播。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable
from typing import TypeVar

from ..core.errors import RunCancelled

__all__ = ["CancellationToken", "race_cancellation", "with_timeout"]

T = TypeVar("T")


class CancellationToken:
    """一次 run 的取消信号。

    可以被多方持有：HTTP 端点收到断开时取消，超时看门狗到期时取消，
    上层用户点取消按钮时取消。所有持有者调 :meth:`cancel` 都幂等。
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason or "已取消"

    def cancel(self, reason: str = "") -> None:
        """发出取消信号。重复调用是安全的，第一次的原因被保留。"""
        if not self._event.is_set():
            self._reason = reason or "已取消"
            self._event.set()

    def raise_if_cancelled(self) -> None:
        """取消了就抛 :class:`RunCancelled`。

        在循环的每一步和每次工具执行前调用——不检查的话，一个正在跑 15 步的
        run 在收到取消后还会继续烧完所有 token。
        """
        if self._event.is_set():
            raise RunCancelled(self.reason)

    async def wait(self) -> None:
        """等到被取消为止。"""
        await self._event.wait()

    def child(self) -> CancellationToken:
        """派生一个子 token。

        父级被取消时子级不会自动跟着取消（那需要额外挂回调），
        所以子 token 只在「同一层级内共享」的场景用。跨层级请直接传同一个 token。
        """
        return CancellationToken()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = f"已取消: {self._reason}" if self.cancelled else "活跃"
        return f"<CancellationToken {state}>"


async def race_cancellation(awaitable: Awaitable[T], token: CancellationToken) -> T:
    """执行 ``awaitable``，但如果 ``token`` 先被取消就放弃它。

    这是让取消**真正生效**的关键：没有它，取消了也还是得等工具跑完才发现没人要结果了。
    被放弃的协程会被 ``cancel()``，这正是 ``run_command`` 里那个 ``finally`` 清理
    子进程的触发点——所以进程不会变成孤儿。
    """
    if token.cancelled:
        # 提前返回时必须把没被消费的协程关掉。不关的话 Python 会留下一个
        # 永不 await 的协程对象，运行时警告只是表象，真正的问题是那段代码
        # 对应的资源（可能已经建立的连接）没人清理。
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        token.raise_if_cancelled()

    work = asyncio.ensure_future(awaitable)
    cancelled = asyncio.ensure_future(token.wait())

    try:
        done, _ = await asyncio.wait(
            {work, cancelled}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if not cancelled.done():
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled

    if work in done:
        return work.result()

    # token 先触发了——停掉还在跑的工作，把取消信号交给上层
    work.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await work
    token.raise_if_cancelled()
    raise RunCancelled(token.reason)  # pragma: no cover - raise_if_cancelled 必然先触发


async def with_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    token: CancellationToken | None = None,
    *,
    what: str = "操作",
) -> T:
    """给一个操作加超时，同时保持对取消的响应。

    超时和取消是两回事：超时是「这次操作太久」，取消是「整个 run 不要了」。
    两者都应该能中断操作，但产生的错误不同，日志里要能分开。
    """
    if timeout is None:
        return await (race_cancellation(awaitable, token) if token else awaitable)

    try:
        if token is not None:
            return await asyncio.wait_for(race_cancellation(awaitable, token), timeout)
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError as exc:
        from ..core.errors import BudgetExceeded

        raise BudgetExceeded(f"{what}超时（{timeout:g} 秒）") from exc
