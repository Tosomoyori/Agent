"""回放模型：按用例里声明的脚本产生响应。

用途是 **hermetic 模式**——不联网、不花钱、结果确定。它评的不是 agent 的能力，
而是**评测框架自己**：判据写对了吗、工作区隔离住了吗、聚合的算法有没有错。
CI 里必须能跑，否则一套没人跑的评测等于没有。

它和 ``tests/conftest.py`` 里的 ``ScriptedModel`` 看着像，但用途不同：
那个是单测替身，要能构造任意畸形响应（坏 JSON、异常、逐 token 的流）来测边界；
这个消费的是评测用例里的**声明式脚本**，只需覆盖「调工具」和「给答复」两种。
硬合并会让两边都不好用。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..core.types import Message, TextBlock, ToolUseBlock
from ..core.usage import Usage
from ..llm.base import ChatModel, ModelCapabilities, StreamChunk, ToolCallDelta
from .case import ScriptStep

__all__ = ["ReplayModel", "build_from_script"]


class ReplayModel(ChatModel):
    """按脚本依次产出响应。脚本跑完还有调用就直接给一个终止答复。

    最后这一点是刻意的：脚本耗尽时抛异常会让评测因为「框架的边界处理」
    而不是「agent 的表现」失败。给一个平淡的终止答复，让判据去判——
    判据本来就会发现该调的工具没调。
    """

    def __init__(
        self,
        steps: list[ScriptStep],
        *,
        name: str = "replay-model",
        usage_per_call: Usage | None = None,
    ) -> None:
        self.steps = list(steps)
        self.name = name
        self.capabilities = ModelCapabilities()
        self.calls = 0
        self._usage = usage_per_call or Usage(input_tokens=100, output_tokens=20)

    def stream(
        self,
        *,
        messages: list[Message],
        tools=None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        self.calls += 1

        step = (
            self.steps.pop(0)
            if self.steps
            else ScriptStep(answer="（脚本已耗尽）")
        )
        return self._generate(step)

    async def _generate(self, step: ScriptStep) -> AsyncIterator[StreamChunk]:
        if step.call is not None:
            name, arguments = step.call
            yield StreamChunk(
                tool_calls=[
                    ToolCallDelta(
                        index=0,
                        id=f"replay_{self.calls}",
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    )
                ],
                finish_reason="tool_calls",
                usage=self._usage,
            )
            return

        yield StreamChunk(text=step.answer or "", usage=self._usage)


def build_from_script(
    steps: list[ScriptStep], *, name: str = "replay-model"
) -> ReplayModel:
    return ReplayModel(steps, name=name)


def reply(text: str) -> Message:  # pragma: no cover - 便捷构造
    return Message.assistant(TextBlock(text=text))


def call(name: str, **arguments: Any) -> Message:  # pragma: no cover - 便捷构造
    return Message.assistant(ToolUseBlock(id="call_1", name=name, input=arguments))
