"""LLM Provider 抽象。

抽象层要抹平的东西分两类，难度完全不同：

* **可以抹平的**：OpenAI / DeepSeek / Qwen / Kimi / GLM 的工具调用请求响应骨架基本一致
  （``tools[].function`` 进、``tool_calls[]`` 出、``role:"tool"`` 回传），改 ``base_url``
  就能切。
* **抹不平的**：流式 tool_call 的 delta 语义、结构化输出的强制等级、缓存机制、
  token 计数字段、思维链的表示方式。这些必须在各自的适配器里单独建模。

所以接口按「能力声明 + 适配器」设计：:class:`ModelCapabilities` 让上层能探测
「这个模型支持并行工具调用吗」而不必 isinstance 判断，:class:`ChatModel` 只规定
最小的方法面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Message
from ..core.usage import Usage


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


@dataclass
class Completion:
    """一次非流式补全的结果。"""

    message: Message
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    #: provider 返回的原始响应，留作排查用。
    raw: Any = None

    @property
    def tool_uses(self):
        return self.message.tool_uses()


class ChatModel(ABC):
    """所有 provider 适配器的基类。"""

    #: 模型 id，例如 ``deepseek-flash``。
    name: str
    capabilities: ModelCapabilities

    @abstractmethod
    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Completion:
        """跑一次补全。``messages`` 是内部内容块模型，由适配器负责转换。"""

    async def aclose(self) -> None:
        """释放底层连接。默认什么都不做。"""
        return None

    async def __aenter__(self) -> ChatModel:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r}>"
