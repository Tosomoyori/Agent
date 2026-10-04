"""重试策略。

三条规则，每条都对应一种真实的踩坑方式：

**一、只重试值得重试的错误。** 429 / 5xx / 408 / 网络抖动重试有意义；400、401、403
重试只会继续失败，还白烧一遍钱。判定依据是 :mod:`agentkit.core.errors` 里的
``retryable`` 标记，不做类型名猜测。

**二、必须关掉 SDK 自带的重试。** 用自定义重试层时如果 SDK 也开着重试，实际次数是
两者相乘——``max_retries=3`` 配上 3 次自定义重试就是 9 次，遇到限流时会把配额耗光。
所以适配器构造 ``AsyncOpenAI`` 时传 ``max_retries=0``。

**三、流式请求只在第一个 chunk 之前可以重试。** 一旦已经有 token 吐给调用方，
中途断掉就不能重试了——重来一次会重复输出内容，而且那部分输入 token 已经计过费。
这不是保守，是正确性：调用方已经看到的东西没法撤回。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from ..core.errors import AgentKitError, is_retryable

__all__ = ["RetryPolicy", "retry_delay", "with_retry", "stream_with_retry"]

T = TypeVar("T")

#: 服务端要求等待的秒数上限。再怎么被限流也不该让用户干等十分钟。
MAX_HONORED_RETRY_AFTER = 60.0


@dataclass(frozen=True)
class RetryPolicy:
    """重试参数。"""

    #: 总尝试次数（含首次）。1 表示不重试。
    max_attempts: int = 3
    #: 退避基准秒数。
    base_delay: float = 0.5
    #: 单次等待上限。
    max_delay: float = 30.0
    #: 是否尊重服务端的 Retry-After 头。
    respect_retry_after: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少是 1")


def retry_delay(
    exc: BaseException,
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> float:
    """算出下一次重试前该等多久。

    退避用 **full jitter**（``random(0, min(cap, base * 2**attempt))``）而不是固定
    的指数退避：多个客户端同时被限流时，固定退避会让它们在同一时刻一起重试，
    形成二次冲击。随机化把重试时刻摊开。

    :param attempt: 已经失败的次数，从 1 开始。
    """
    retry_after = getattr(exc, "retry_after", None)
    if policy.respect_retry_after and retry_after is not None:
        # 服务端明确说了等多久就听它的——但设个上限，别被一个离谱的值挂住
        return max(0.0, min(float(retry_after), MAX_HONORED_RETRY_AFTER))

    ceiling = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
    generator = rng or random
    return generator.uniform(0, ceiling)


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[BaseException, int, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """执行 ``operation``，按策略重试。

    ``on_retry(exc, attempt, delay)`` 在每次决定重试后调用，供上层记录日志或发事件。
    """
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except AgentKitError as exc:
            if not is_retryable(exc) or attempt >= policy.max_attempts:
                raise
            last_error = exc

            delay = retry_delay(exc, attempt, policy)
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            await sleep(delay)

    # 循环必然在最后一次尝试处 return 或 raise，走到这里说明逻辑有洞
    raise last_error if last_error else RuntimeError("重试逻辑异常")  # pragma: no cover


async def stream_with_retry(
    make_stream: Callable[[], AsyncIterator[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[BaseException, int, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[T]:
    """流式版本的重试。

    **只在第一个元素产出之前允许重试。** 一旦有内容吐给调用方，中途的错误直接抛出——
    重来一次会重复输出，而且那部分输入 token 已经计过费了。调用方已经看到的东西
    没法撤回，假装没发生过只会更糟。

    ``make_stream`` 是一个**每次调用都新建一条流**的工厂，不能传入已经创建好的流对象
    （异步迭代器只能消费一次）。
    """
    for attempt in range(1, policy.max_attempts + 1):
        emitted = False
        try:
            async for item in make_stream():
                emitted = True
                yield item
            return
        except AgentKitError as exc:
            if emitted:
                # 已经吐过内容了，重试会造成重复输出与重复计费
                raise
            if not is_retryable(exc) or attempt >= policy.max_attempts:
                raise

            delay = retry_delay(exc, attempt, policy)
            if on_retry is not None:
                on_retry(exc, attempt, delay)
            await sleep(delay)
