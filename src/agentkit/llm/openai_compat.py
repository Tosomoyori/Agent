"""OpenAI 兼容适配器（覆盖 DeepSeek / Qwen / Kimi / GLM）。

这是 ``llm`` 包里代码量最大的一块，因为「OpenAI 兼容」只统一了最表层。真正需要
单独处理的差异是：

* **工具调用形状**：内部是 assistant 消息里的 content block，OpenAI 是
  ``assistant.tool_calls[]``（参数是**字符串**）+ 独立的 ``role:"tool"`` 消息；
* **思维链**：OpenAI 没有这个概念，DeepSeek 用自己的 ``reasoning_content`` 字段承载，
  且**要求在多轮工具调用时原样回传**，否则下一次请求 400。实测中思维链占了输出
  token 的绝大多数（"数到五"产生了 1012 个 reasoning token，正文只有 5 个 token）；
* **usage 字段语义**：缓存命中数是 ``prompt_tokens`` 的**子集**，
  reasoning 是 ``completion_tokens`` 的**子集**，不做相减会重复计费；
* **工具参数可能是坏 JSON**：模型偶发会给出一段不是合法 JSON 的 ``arguments``。

转换逻辑全部写成**纯函数**并单独导出，这样单测可以不碰网络就覆盖全部分支。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

import openai

from ..core.errors import (
    AgentKitError,
    AuthenticationError,
    ContextLengthExceeded,
    InvalidRequestError,
    LLMError,
    RateLimitError,
    TransientLLMError,
)
from ..core.types import Message, ToolSchema
from ..core.usage import Usage
from ._json import parse_tool_arguments  # noqa: F401 - 对外重新导出，历史调用方在用
from .base import ChatModel, ModelCapabilities, StreamChunk, ToolCallDelta
from .retry import RetryPolicy, stream_with_retry

__all__ = [
    "OpenAICompatModel",
    "to_openai_messages",
    "to_openai_tools",
    "to_stream_chunk",
    "normalize_usage",
    "parse_tool_arguments",
    "map_openai_error",
    "extract_retry_after",
]


# ============================================================ 内部 → OpenAI


def to_openai_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """把内部内容块消息摊平成 OpenAI Chat Completions 的 messages。

    三处形状差异在这里处理：

    1. ``ToolResultBlock`` 在内部是 user 消息的 content block，OpenAI 要的是独立的
       ``role:"tool"`` 消息，每个结果一条；
    2. ``ToolUseBlock`` 要挂到 ``assistant.tool_calls[]``，且 ``arguments`` 必须
       序列化成字符串；
    3. ``ReasoningBlock`` 映射到 DeepSeek 的 ``reasoning_content`` 字段。
    """
    out: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            out.append({"role": "system", "content": msg.text()})

        elif msg.role == "user":
            text = msg.text()
            results = msg.tool_results()
            if text:
                out.append({"role": "user", "content": text})
            for r in results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": r.tool_use_id,
                        "content": r.content,
                    }
                )
            # 空 user 消息也保留，否则模型会看到断掉的一轮
            if not text and not results:
                out.append({"role": "user", "content": ""})

        else:  # assistant
            entry: dict[str, Any] = {"role": "assistant", "content": msg.text() or None}

            # DeepSeek 要求 reasoning_content 在多轮工具调用中原样回传。
            # 不传会 400；传空串则等价于没传。
            if reasoning := msg.reasoning_text():
                entry["reasoning_content"] = reasoning

            if uses := msg.tool_uses():
                entry["tool_calls"] = [
                    {
                        "id": u.id,
                        "type": "function",
                        "function": {
                            "name": u.name,
                            # ensure_ascii=False：中文参数不必转义成 \uXXXX，
                            # 省 token 也更好读日志
                            "arguments": json.dumps(u.input, ensure_ascii=False),
                        },
                    }
                    for u in uses
                ]

            out.append(entry)

    return out


def to_openai_tools(schemas: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    """把 provider 中立的工具契约转成 OpenAI 的 tool 定义。"""
    return [s.openai_tool() for s in schemas]


# ============================================================ OpenAI → 内部


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """从 pydantic 对象或 dict 里取字段。

    OpenAI SDK 返回的是 pydantic 模型，扩展字段（如 ``reasoning_content``）
    落在 ``model_extra`` 里；测试里我们喂的是原始 dict。两种都要能读。
    """
    if obj is None:
        return default

    if isinstance(obj, Mapping):
        return obj.get(key, default)

    value = getattr(obj, key, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, Mapping):
            value = extra.get(key)

    return default if value is None else value


def to_stream_chunk(raw: Any) -> StreamChunk:
    """把一个原始 SSE chunk 归一化成 :class:`StreamChunk`。

    两种情况要分开处理：

    * **带 choices 的常规 chunk**：取 ``delta`` 里的正文、思维链和工具调用增量；
    * **只有 usage 的收尾 chunk**：OpenAI 协议里这种 chunk 的 ``choices`` 是**空列表**
      （实测 DeepSeek 同样如此），不能当成"没有内容"直接丢掉。
    """
    usage = _get(raw, "usage")
    choices = _get(raw, "choices") or []

    if not choices:
        return StreamChunk(usage=normalize_usage(usage) if usage else None)

    choice = choices[0]
    chunk = StreamChunk(finish_reason=_get(choice, "finish_reason"))

    delta = _get(choice, "delta")
    if delta is not None:
        chunk.text = _get(delta, "content") or ""
        # reasoning_content 不在标准字段上，落在 model_extra 里
        chunk.reasoning = _get(delta, "reasoning_content") or ""

        for call in _get(delta, "tool_calls") or []:
            function = _get(call, "function") or {}
            chunk.tool_calls.append(
                ToolCallDelta(
                    index=_get(call, "index", 0),
                    # id / name 只在首个增量出现，后续为 None——
                    # 这里保持 None 让累加器知道"这次没有新值"，而不是覆盖成空串
                    id=_get(call, "id"),
                    name=_get(function, "name"),
                    arguments=_get(function, "arguments") or "",
                )
            )

    if usage:
        chunk.usage = normalize_usage(usage)

    return chunk


def pick_from(obj: Any, key: str) -> int:
    """从嵌套的 details 对象里取一个整数字段。"""
    value = _get(obj, key)
    return int(value) if value is not None else 0


def normalize_usage(raw: Any) -> Usage:
    """把各家的 usage 字段归一成 :class:`Usage`。

    要同时认两种命名体系（OpenAI 系 / Anthropic 系），并且**区分子集关系**：
    ``cached_tokens`` 是输入的子集，``reasoning_tokens`` 是输出的子集。
    """

    def pick(*names: str) -> int:
        """取第一个存在且非 None 的字段值。"""
        for name in names:
            value = _get(raw, name)
            if value is not None:
                return int(value)
        return 0

    if raw is None:
        return Usage()

    input_tokens = pick("prompt_tokens", "input_tokens")
    output_tokens = pick("completion_tokens", "output_tokens")

    # 缓存命中数：各家位置不一致，按常见程度依次尝试
    cached = pick("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens")
    if not cached:
        for details_key in ("prompt_tokens_details", "input_tokens_details"):
            if details := _get(raw, details_key):
                cached = pick_from(details, "cached_tokens")
                if cached:
                    break

    reasoning = 0
    for details_key in ("completion_tokens_details", "output_tokens_details"):
        if details := _get(raw, details_key):
            reasoning = pick_from(details, "reasoning_tokens")
            if reasoning:
                break

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
    )


# ============================================================ 错误映射


def extract_retry_after(exc: Exception) -> float | None:
    """从 SDK 异常的响应头里读出服务端要求的等待秒数。

    BUG 教训：不提取这个值，重试就变成"自己猜退避"——服务端说 30 秒后再来，
    客户端 1 秒后重试 5 次，既没用又白烧配额。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    # 毫秒版本优先：它是较新的标准，精度更高
    for key, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(key)
        if raw is None:
            continue
        try:
            return float(raw) * scale
        except (TypeError, ValueError):
            continue  # Retry-After 也可能是 HTTP 日期格式，这里不解析
    return None


def map_openai_error(exc: Exception) -> AgentKitError:
    """把 OpenAI SDK 的异常映射到本项目的错误分类学。

    这张表决定了重试与否，所以「不确定就当成不可重试」——猜错会把一次性的
    错误变成重复扣费。
    """
    if isinstance(exc, AgentKitError):
        return exc  # 已经是自己的错误，别再包一层

    mapped: AgentKitError
    if isinstance(exc, openai.RateLimitError):
        mapped = RateLimitError(str(exc))
    elif isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        mapped = AuthenticationError(str(exc))
    elif isinstance(exc, openai.BadRequestError):
        text = str(exc).lower()
        if "context length" in text or "maximum context" in text or "too long" in text:
            mapped = ContextLengthExceeded(str(exc))
        else:
            mapped = InvalidRequestError(str(exc))
    elif isinstance(
        exc,
        openai.APITimeoutError | openai.APIConnectionError | openai.InternalServerError,
    ):
        mapped = TransientLLMError(str(exc))
    elif isinstance(exc, openai.APIStatusError):
        # 其余 4xx 归为不可重试，5xx 归为可重试
        mapped = (
            TransientLLMError(str(exc))
            if 500 <= exc.status_code < 600
            else LLMError(str(exc))
        )
    else:
        mapped = LLMError(str(exc))

    mapped.retry_after = extract_retry_after(exc)
    return mapped


# ============================================================ 适配器


class OpenAICompatModel(ChatModel):
    """走 OpenAI 兼容 ``/chat/completions`` 的模型适配器。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        retry_policy: RetryPolicy | None = None,
        capabilities: ModelCapabilities | None = None,
        default_headers: dict[str, str] | None = None,
        on_retry: Callable[[BaseException, int, float], None] | None = None,
    ) -> None:
        self.name = model
        self.capabilities = capabilities or ModelCapabilities()
        self.retry_policy = retry_policy or RetryPolicy()
        self._on_retry = on_retry
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            # 固定传 0：重试由 agentkit.llm.retry 自己管。
            # 两边都开会让实际重试次数相乘（见 retry 模块的说明）。
            max_retries=0,
            default_headers=default_headers,
        )
        self._base_url = base_url

    async def stream(  # type: ignore[override]
        self,
        *,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        payload: dict[str, Any] = {
            "model": self.name,
            "messages": to_openai_messages(messages),
            "stream": True,
            # 实测 DeepSeek 默认就会下发 usage，但 OpenAI 需要显式开这个开关。
            # 统一打开，让「有没有 usage」不再是各家的差异。
            "stream_options": {"include_usage": True},
            **kwargs,
        }

        if tools:
            if not self.capabilities.tool_calling:
                raise InvalidRequestError(f"模型 {self.name} 不支持工具调用")
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = tool_choice

        async def make_stream() -> AsyncIterator[StreamChunk]:
            """每次重试都重新发起请求——异步迭代器只能消费一次，不能复用。"""
            try:
                raw_stream = await self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - 统一收敛到自己的错误分类学
                raise map_openai_error(exc) from exc

            try:
                async for raw in raw_stream:
                    yield to_stream_chunk(raw)
            except Exception as exc:  # noqa: BLE001 - 流中途断掉也要归一化
                raise map_openai_error(exc) from exc
            finally:
                # 调用方提前中断（取消 / 超预算）时也要把连接放掉，
                # 否则连接池会被慢慢耗干。
                await raw_stream.close()

        async for chunk in stream_with_retry(
            make_stream, self.retry_policy, on_retry=self._on_retry
        ):
            yield chunk

    async def aclose(self) -> None:
        await self._client.close()
