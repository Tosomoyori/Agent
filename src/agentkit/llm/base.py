"""LLM Provider 抽象。

抽象层要抹平的东西分两类，难度完全不同：

* **可以抹平的**：OpenAI / DeepSeek / Qwen / Kimi / GLM 的工具调用请求响应骨架基本一致
  （``tools[].function`` 进、``tool_calls[]`` 出、``role:"tool"`` 回传），改 ``base_url``
  就能切。
* **抹不平的**：流式 tool_call 的 delta 语义、结构化输出的强制等级、缓存机制、
  token 计数字段、思维链的表示方式。这些必须在各自的适配器里单独建模。

所以接口按「能力声明 + 适配器」设计：:class:`ModelCapabilities` 让上层能探测
「这个模型支持并行工具调用吗」而不必 isinstance 判断。

**``complete()`` 由 ``stream()`` 实现**——不是两个独立的调用路径。否则「流式返回的
消息」和「非流式返回的消息」会各写一遍组装逻辑，迟早出现只有一边正确的情况。
代价是即使不需要流式也要走 SSE 解析，这点开销换来的是只有一条路径要维护。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Message, ReasoningBlock, TextBlock, ToolUseBlock
from ..core.usage import Usage
from ._json import join_argument_fragments, parse_tool_arguments

__all__ = [
    "ChatModel",
    "Completion",
    "ModelCapabilities",
    "StreamAccumulator",
    "StreamChunk",
    "ToolCallDelta",
    "accumulate",
]


@dataclass(frozen=True)
class ModelCapabilities:
    """模型能力声明。

    上层的降级逻辑读这里，而不是去猜模型名。例如：不支持 ``parallel_tool_calls``
    的 provider（Qwen 默认关闭）在发出多个 tool_use 时行为不同；不支持
    ``json_object`` 的（GLM）需要走客户端校验。
    """

    context_window: int = 128_000
    max_output_tokens: int = 8_192
    tool_calling: bool = True
    parallel_tool_calls: bool = True
    streaming: bool = True
    streaming_tool_calls: bool = True
    json_object: bool = False
    json_schema: bool = False
    reasoning: bool = False
    image_input: bool = False
    #: usage 是否随流式响应下发。OpenAI 需要显式开 ``stream_options``，
    #: 而实测 DeepSeek 默认就给——所以它必须是能力声明而不是硬编码假设。
    usage_in_stream: bool = True


@dataclass
class ToolCallDelta:
    """流式响应里一段工具调用增量。

    ``id`` / ``name`` 是**赋值**语义，``arguments`` 是**追加**语义。混起来用
    ``+=`` 会把 id 拼成一串垃圾——实测里 id 只在首个增量出现，后续为 ``None``。
    """

    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass
class StreamChunk:
    """流式响应里的一个增量，已由适配器归一化。"""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass
class Completion:
    """一次补全的完整结果。"""

    message: Message
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    #: provider 返回的原始响应，留作排查用。
    raw: Any = None

    @property
    def tool_uses(self):
        return self.message.tool_uses()


@dataclass
class _PartialCall:
    """累加中的单个工具调用。"""

    index: int
    id: str | None = None
    name: str | None = None
    fragments: list[str] = field(default_factory=list)


class StreamAccumulator:
    """把流式增量还原成一条完整的 assistant 消息。

    要处理三处 provider 行为（都是实测出来的，不是照文档猜的）：

    1. ``id`` / ``name`` 只在首个 tool_call 增量里出现，后续只发 ``arguments``。
       所以这两个是赋值不是追加，用 ``+=`` 会拼出重复的垃圾。
    2. **并行调用**靠 ``index`` 区分，必须按 index 分组——按出现顺序分组会把
       两个调用的参数混在一起。
    3. usage 只在最后一个 chunk 出现，且可能整条流都没有。
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, _PartialCall] = {}
        self._order: list[int] = []
        self.finish_reason: str | None = None
        self.usage: Usage = Usage()
        #: 累加过程中观察到的异常情况，例如走了累积重发的退路。
        self.notes: list[str] = []

    def feed(self, chunk: StreamChunk) -> None:
        if chunk.text:
            self._text.append(chunk.text)
        if chunk.reasoning:
            self._reasoning.append(chunk.reasoning)

        for delta in chunk.tool_calls:
            call = self._calls.get(delta.index)
            if call is None:
                call = _PartialCall(index=delta.index)
                self._calls[delta.index] = call
                self._order.append(delta.index)

            # 赋值语义：只在非 None 时覆盖，绝不做 +=
            if delta.id is not None:
                call.id = delta.id
            if delta.name is not None:
                call.name = delta.name
            if delta.arguments:
                call.fragments.append(delta.arguments)

        if chunk.finish_reason is not None:
            self.finish_reason = chunk.finish_reason
        if chunk.usage is not None:
            self.usage = chunk.usage

    @property
    def text(self) -> str:
        return "".join(self._text)

    @property
    def reasoning(self) -> str:
        return "".join(self._reasoning)

    def message(self) -> Message:
        """组装成内部的内容块消息。"""
        blocks: list[Any] = []
        if self.reasoning:
            blocks.append(ReasoningBlock(text=self.reasoning))
        if self.text:
            blocks.append(TextBlock(text=self.text))

        for index in self._order:
            call = self._calls[index]
            raw, note = join_argument_fragments(call.fragments)
            if note:
                self.notes.append(f"工具调用 {call.name or index}: {note}")

            arguments, unparsed = parse_tool_arguments(raw)
            blocks.append(
                ToolUseBlock(
                    id=call.id or f"call_{index}",
                    name=call.name or "",
                    input=arguments,
                    # 拼不成合法 JSON 时保留原文，让引擎把它当作一次参数错误
                    # 回灌给模型，而不是静默地传一个空参数过去
                    raw_arguments=unparsed,
                )
            )

        return Message(role="assistant", content=blocks)


async def accumulate(stream: AsyncIterator[StreamChunk]) -> Completion:
    """消费整条流，汇聚成 :class:`Completion`。"""
    accumulator = StreamAccumulator()
    async for chunk in stream:
        accumulator.feed(chunk)

    return Completion(
        message=accumulator.message(),
        finish_reason=accumulator.finish_reason or "stop",
        usage=accumulator.usage,
    )


class ChatModel(ABC):
    """所有 provider 适配器的基类。"""

    #: 模型 id，例如 ``deepseek-flash``。
    name: str
    capabilities: ModelCapabilities

    @abstractmethod
    def stream(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """流式跑一次补全，逐个 yield 已归一化的增量。

        注意这里返回的是（异步）迭代器而不是协程——调用方需要 ``async for``，
        不是 ``await``。
        """

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Completion:
        """跑一次补全并返回完整结果。

        默认实现是「把流消费完再汇聚」，保证流式与非流式**共用同一条组装路径**。
        需要专门优化的适配器可以覆盖它。
        """
        return await accumulate(
            self.stream(messages=messages, tools=tools, tool_choice=tool_choice, **kwargs)
        )

    async def aclose(self) -> None:
        """释放底层连接。默认什么都不做。"""
        return None

    async def __aenter__(self) -> ChatModel:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r}>"
