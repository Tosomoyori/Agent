"""OpenAI 兼容适配器（覆盖 DeepSeek / Qwen / Kimi / GLM）。

这是 ``llm`` 包里代码量最大的一块，因为「OpenAI 兼容」只统一了最表层。真正需要
单独处理的差异是：

* **工具调用形状**：内部是 assistant 消息里的 content block，OpenAI 是
  ``assistant.tool_calls[]``（参数是**字符串**）+ 独立的 ``role:"tool"`` 消息。
  两个方向都要转；
* **思维链**：OpenAI 没有这个概念，DeepSeek 用自己的 ``reasoning_content`` 字段承载，
  且**要求在多轮工具调用时原样回传**，否则下一次请求 400；
* **usage 字段语义**：缓存命中数是 ``prompt_tokens`` 的**子集**，
  reasoning 是 ``completion_tokens`` 的**子集**，不做相减会重复计费；
* **工具参数可能是坏 JSON**：模型偶发给出一段不是合法 JSON 的 ``arguments``。

转换逻辑全部写成**纯函数**并单独导出，这样单测可以不碰网络就覆盖全部分支。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
from ..core.ids import new_call_id
from ..core.types import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolSchema,
    ToolUseBlock,
)
from ..core.usage import Usage
from .base import ChatModel, Completion, ModelCapabilities

__all__ = [
    "OpenAICompatModel",
    "to_openai_messages",
    "to_openai_tools",
    "from_openai_message",
    "normalize_usage",
    "parse_tool_arguments",
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


def parse_tool_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """解析模型给出的工具参数。

    返回 ``(参数, 无法解析的原文)``，**不抛异常**。

    理由：「模型给了一段不是 JSON 的参数」是可恢复的错误——把它当作一次参数校验失败
    回灌给模型，让它修正重试，比中断整个 run 划算。所以这里把原文一并交出去，
    由上层决定怎么在回灌消息里说明。

    模型有时用 ``null`` 或空串表示「这个工具没有参数」，那不是错误。
    """
    if raw is None:
        return {}, None

    text = raw.strip()
    if not text or text in ("null", "none", "{}"):
        return {}, None

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}, raw

    if isinstance(value, dict):
        return value, None

    # 合法 JSON 但不是对象（比如模型直接给了个字符串或数组）
    return {}, raw


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


def from_openai_message(raw: Any) -> Message:
    """把 OpenAI 的 assistant 消息还原成内部的内容块模型。"""
    blocks: list[Any] = []

    if reasoning := _get(raw, "reasoning_content"):
        blocks.append(ReasoningBlock(text=reasoning))

    if content := _get(raw, "content"):
        blocks.append(TextBlock(text=content))

    for call in _get(raw, "tool_calls") or []:
        fn = _get(call, "function") or {}
        arguments, unparsed = parse_tool_arguments(_get(fn, "arguments"))
        blocks.append(
            ToolUseBlock(
                id=_get(call, "id") or new_call_id(),
                name=_get(fn, "name") or "",
                input=arguments,
                raw_arguments=unparsed,
            )
        )

    return Message(role="assistant", content=blocks)


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


def pick_from(obj: Any, key: str) -> int:
    """从嵌套的 details 对象里取一个整数字段。"""
    value = _get(obj, key)
    return int(value) if value is not None else 0


# ============================================================ 错误映射


def map_openai_error(exc: Exception) -> AgentKitError:
    """把 OpenAI SDK 的异常映射到本项目的错误分类学。

    这张表决定了重试与否，所以「不确定就当成不可重试」——猜错会把一次性的
    错误变成重复扣费。
    """
    if isinstance(exc, openai.RateLimitError):
        return RateLimitError(str(exc))
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return AuthenticationError(str(exc))
    if isinstance(exc, openai.BadRequestError):
        text = str(exc).lower()
        if "context length" in text or "maximum context" in text or "too long" in text:
            return ContextLengthExceeded(str(exc))
        return InvalidRequestError(str(exc))
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return TransientLLMError(str(exc))
    if isinstance(exc, openai.InternalServerError):
        return TransientLLMError(str(exc))
    if isinstance(exc, openai.APIStatusError):
        # 其余 4xx 归为不可重试，5xx 归为可重试
        if 500 <= exc.status_code < 600:
            return TransientLLMError(str(exc))
        return LLMError(str(exc))
    if isinstance(exc, openai.OpenAIError):
        return LLMError(str(exc))
    return LLMError(str(exc))


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
        max_retries: int = 3,
        capabilities: ModelCapabilities | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = model
        self.capabilities = capabilities or ModelCapabilities()
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            # 现在由 SDK 负责重试（它认得 Retry-After 并做指数退避）。
            # Phase 2 接入自带的重试层时，这里必须改成 0，否则两边次数相乘。
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._base_url = base_url

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.name,
            "messages": to_openai_messages(messages),
            **kwargs,
        }

        if tools:
            if not self.capabilities.tool_calling:
                raise InvalidRequestError(f"模型 {self.name} 不支持工具调用")
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = tool_choice

        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - 统一收敛到自己的错误分类学
            raise map_openai_error(exc) from exc

        if not response.choices:
            raise LLMError(f"{self.name} 返回了空的 choices")

        choice = response.choices[0]
        return Completion(
            message=from_openai_message(choice.message),
            finish_reason=choice.finish_reason or "stop",
            usage=normalize_usage(getattr(response, "usage", None)),
            raw=response,
        )

    async def aclose(self) -> None:
        await self._client.close()
