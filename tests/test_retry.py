"""重试策略。

三个关键行为：

1. 只重试标记为可重试的错误——猜错会把一次性的错误变成重复扣费；
2. 尊重服务端的 ``Retry-After``——自己猜退避既没用又费配额；
3. **流式请求只在第一个 chunk 之前可以重试**——已经吐给调用方的内容收不回来。
"""

from __future__ import annotations

import random

import pytest
from openai import APIStatusError
from openai import RateLimitError as OpenAIRateLimit

from agentkit.core.errors import (
    AuthenticationError,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    TransientLLMError,
    is_retryable,
)
from agentkit.llm.openai_compat import extract_retry_after, map_openai_error
from agentkit.llm.retry import RetryPolicy, retry_delay, stream_with_retry, with_retry


class TestRetryability:
    @pytest.mark.parametrize("exc", [RateLimitError("x"), TransientLLMError("x")])
    def test_retryable_errors(self, exc):
        assert is_retryable(exc)

    @pytest.mark.parametrize(
        "exc", [AuthenticationError("x"), InvalidRequestError("x"), LLMError("x")]
    )
    def test_non_retryable_errors(self, exc):
        """认证失败和请求非法重试只会继续失败，还白烧钱。"""
        assert not is_retryable(exc)

    def test_unknown_error_is_not_retried(self):
        """不确定就当成不可重试——猜错会把一次性错误变成重复扣费。"""
        assert not is_retryable(RuntimeError("莫名其妙的错误"))


class TestRetryDelay:
    def test_backoff_grows_and_is_capped(self):
        policy = RetryPolicy(base_delay=0.5, max_delay=4.0)
        # 用固定 rng 让结果可复现：uniform(0, ceiling) 取上界即 ceiling
        rng = random.Random(0)

        ceilings = []
        for attempt in (1, 2, 3, 4, 5, 6):
            # 多次取样取最大值，逼近 ceiling
            delays = [retry_delay(LLMError("x"), attempt, policy, rng=rng) for _ in range(200)]
            ceilings.append(max(delays))

        assert ceilings[0] < ceilings[1] < ceilings[2]
        assert max(ceilings) <= 4.0 + 1e-9

    def test_retry_after_header_wins(self):
        """服务端说了等多久就听它的。"""
        exc = RateLimitError("限流")
        exc.retry_after = 12.0
        assert retry_delay(exc, 1, RetryPolicy()) == 12.0

    def test_retry_after_is_capped(self):
        """但设个上限，别被一个离谱的值把进程挂住。"""
        exc = RateLimitError("限流")
        exc.retry_after = 9999.0
        assert retry_delay(exc, 1, RetryPolicy()) <= 60.0

    def test_retry_after_ignored_when_policy_says_so(self):
        exc = RateLimitError("限流")
        exc.retry_after = 12.0
        policy = RetryPolicy(respect_retry_after=False, base_delay=1.0, max_delay=1.0)
        assert retry_delay(exc, 1, policy) <= 1.0

    def test_policy_rejects_zero_attempts(self):
        with pytest.raises(ValueError, match="至少是 1"):
            RetryPolicy(max_attempts=0)


class TestWithRetry:
    async def test_succeeds_after_transient_failures(self):
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise TransientLLMError("网络抖动")
            return "成功"

        result = await with_retry(
            flaky, RetryPolicy(max_attempts=3), sleep=_no_sleep
        )
        assert result == "成功"
        assert len(attempts) == 3

    async def test_gives_up_after_max_attempts(self):
        async def always_fails():
            raise TransientLLMError("一直失败")

        with pytest.raises(TransientLLMError):
            await with_retry(
                always_fails, RetryPolicy(max_attempts=2), sleep=_no_sleep
            )

    async def test_non_retryable_error_fails_immediately(self):
        """不该重试的错误一次都不该多试。"""
        attempts = []

        async def bad_request():
            attempts.append(1)
            raise InvalidRequestError("请求非法")

        with pytest.raises(InvalidRequestError):
            await with_retry(bad_request, RetryPolicy(max_attempts=5), sleep=_no_sleep)
        assert len(attempts) == 1

    async def test_on_retry_callback_reports_each_attempt(self):
        events = []

        async def flaky():
            if len(events) < 2:
                raise TransientLLMError("抖一下")
            return "好"

        await with_retry(
            flaky,
            RetryPolicy(max_attempts=3),
            on_retry=lambda exc, attempt, delay: events.append((attempt, delay)),
            sleep=_no_sleep,
        )
        assert [attempt for attempt, _ in events] == [1, 2]


class TestStreamWithRetry:
    async def test_retries_before_the_first_chunk(self):
        """第一个 chunk 之前出错可以安全重试——调用方还没看到任何东西。"""
        calls = []

        async def make_stream():
            calls.append(1)
            if len(calls) < 2:
                raise TransientLLMError("建连失败")
                yield  # pragma: no cover
            yield "内容"

        items = [
            item
            async for item in stream_with_retry(
                make_stream, RetryPolicy(max_attempts=3), sleep=_no_sleep
            )
        ]
        assert items == ["内容"]
        assert len(calls) == 2

    async def test_does_not_retry_after_emitting(self):
        """**已经吐过内容就不能重试了。**

        重来一次会重复输出，而且那部分输入 token 已经计过费。调用方已经看到的
        东西没法撤回，假装没发生过只会更糟。
        """
        calls = []

        async def make_stream():
            calls.append(1)
            yield "第一段"
            raise TransientLLMError("中途断了")

        received = []
        with pytest.raises(TransientLLMError):
            async for item in stream_with_retry(
                make_stream, RetryPolicy(max_attempts=5), sleep=_no_sleep
            ):
                received.append(item)

        assert received == ["第一段"]
        assert len(calls) == 1  # 只发了一次请求，没有重试

    async def test_non_retryable_error_is_not_retried(self):
        calls = []

        async def make_stream():
            calls.append(1)
            raise InvalidRequestError("请求非法")
            yield  # pragma: no cover

        with pytest.raises(InvalidRequestError):
            async for _ in stream_with_retry(
                make_stream, RetryPolicy(max_attempts=5), sleep=_no_sleep
            ):
                pass
        assert len(calls) == 1


class TestErrorMapping:
    def test_rate_limit_is_retryable(self):
        exc = OpenAIRateLimit(
            "限流", response=_fake_response(429), body=None
        )
        assert isinstance(map_openai_error(exc), RateLimitError)

    def test_server_error_is_transient(self):
        exc = APIStatusError("boom", response=_fake_response(503), body=None)
        assert isinstance(map_openai_error(exc), TransientLLMError)

    def test_client_error_is_not_retryable(self):
        exc = APIStatusError("bad", response=_fake_response(404), body=None)
        mapped = map_openai_error(exc)
        assert not is_retryable(mapped)

    def test_already_mapped_error_is_not_rewrapped(self):
        original = RateLimitError("已经是自己的错误")
        assert map_openai_error(original) is original

    def test_retry_after_header_is_extracted(self):
        exc = OpenAIRateLimit(
            "限流", response=_fake_response(429, {"retry-after": "7"}), body=None
        )
        mapped = map_openai_error(exc)
        assert mapped.retry_after == 7.0

    def test_retry_after_ms_is_preferred(self):
        exc = OpenAIRateLimit(
            "限流",
            response=_fake_response(429, {"retry-after": "7", "retry-after-ms": "1500"}),
            body=None,
        )
        assert map_openai_error(exc).retry_after == pytest.approx(1.5)

    def test_absent_header_yields_none(self):
        exc = OpenAIRateLimit("限流", response=_fake_response(429), body=None)
        assert extract_retry_after(exc) is None

    def test_malformed_header_is_ignored(self):
        """Retry-After 也可能是 HTTP 日期格式，解析不了就当没有。"""
        exc = OpenAIRateLimit(
            "限流",
            response=_fake_response(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            body=None,
        )
        assert extract_retry_after(exc) is None


# ---------------------------------------------------------------- 辅助


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.headers = headers
        self.request = None


def _fake_response(status_code: int, headers: dict[str, str] | None = None):
    import httpx

    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
    )


async def _no_sleep(_seconds: float) -> None:
    """把等待去掉，测试不必真的睡。"""
    return None
