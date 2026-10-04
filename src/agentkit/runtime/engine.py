"""推理引擎——原生 tool calling 的循环。

循环本身只有四步，且**没有任何文本解析**：

1. 把消息历史发给模型（附带工具 schema）；
2. 模型要么给最终答复，要么给出一个或多个 ``tool_use``；
3. 有 ``tool_use`` 就执行，把结果打包成一条 user 消息追加回去；
4. 回到第 1 步。

对比旧实现的 ``ReActEngine.solve()``：那里每一轮都要用正则从模型输出里抠 ```json```
块、``json.loads``、失败再用 ``ast.literal_eval`` 兜底、再失败就把原始输出当成
observation 塞回去。整条路径有四个静默降级点，每个都会让模型的错误被吞掉。
现在这些全消失了——格式由 API 保证。

引擎**只 yield 事件**，不返回值。原因见 :mod:`agentkit.core.events` 的模块文档。
"""

from __future__ import annotations

import itertools
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import AgentKitError, RunCancelled
from ..core.events import (
    ReasoningDelta,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    StepStarted,
    TextDelta,
    ToolCallStarted,
    UsageReported,
)
from ..core.events import (
    ToolResult as ToolResultEvent,
)
from ..core.ids import new_run_id
from ..core.types import Message, ToolResultBlock, validate_conversation
from ..core.usage import ZERO_USAGE, Usage, estimate_cost, pricing_for
from ..llm.base import ChatModel
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry
from .prompts import render_system_prompt

__all__ = ["Engine", "RunResult", "collect"]

#: 工具返回坏 JSON 参数时的回灌文案。写得具体，模型才知道该怎么改。
_BAD_JSON_TEMPLATE = (
    "你为工具 {name!r} 提供的参数不是合法的 JSON，无法解析。\n"
    "你给的原文是：{raw}\n"
    "请重新调用该工具，确保参数是合法的 JSON 对象。"
)


@dataclass
class RunResult:
    """一次 run 的最终状态。由 :func:`collect` 从事件流里汇聚出来。"""

    run_id: str
    text: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    currency: str = ""
    duration_ms: int = 0
    error: str | None = None
    error_type: str | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


class Engine:
    """驱动 tool-calling 循环的引擎。"""

    def __init__(
        self,
        model: ChatModel,
        tools: ToolRegistry,
        *,
        workspace: Path,
        name: str = "agent",
        system_prompt: str | None = None,
        max_steps: int = 15,
        temperature: float | None = None,
        max_tokens: int | None = None,
        auto_approve: bool = True,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = Path(workspace).resolve()
        self.name = name
        self.max_steps = max_steps
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.auto_approve = auto_approve

        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else render_system_prompt(workspace=self.workspace, tool_names=tools.names())
        )

    # ------------------------------------------------------------ 主入口

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[Message] | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        """跑一次完整的推理循环，逐个 yield 事件。"""
        run_id = run_id or new_run_id()
        started_at = time.perf_counter()

        counter = itertools.count(1)

        def stamp(event: RunEvent) -> RunEvent:
            event.seq = next(counter)
            event.run_id = run_id
            return event

        ctx = ToolContext(
            workspace=self.workspace,
            run_id=run_id,
            auto_approve=self.auto_approve,
        )

        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message.system(self.system_prompt))
        messages.extend(history or [])
        messages.append(Message.user(user_input))

        yield stamp(
            RunStarted(agent=self.name, model=self.model.name, input=user_input)
        )

        schemas = self.tools.schemas() if len(self.tools) else None
        generation = self._generation_kwargs()
        accumulated = ZERO_USAGE

        for step in range(1, self.max_steps + 1):
            yield stamp(StepStarted(step=step))

            # 每一轮都核对配对不变量。开销可以忽略，但能在产生孤儿 tool_result
            # 的第一时间定位到是哪一步写坏的，而不是等 API 报一个难懂的 400。
            validate_conversation(messages)

            try:
                completion = await self.model.complete(
                    messages=messages, tools=schemas, **generation
                )
            except RunCancelled:
                yield stamp(
                    RunFailed(
                        error_type="RunCancelled", message="run 已取消", steps=step - 1
                    )
                )
                return
            except AgentKitError as exc:
                yield stamp(
                    RunFailed(
                        error_type=type(exc).__name__,
                        message=str(exc),
                        steps=step - 1,
                    )
                )
                return

            accumulated = accumulated + completion.usage
            yield stamp(
                UsageReported(
                    step=step, usage=completion.usage, cumulative=accumulated
                )
            )

            assistant = completion.message
            messages.append(assistant)

            # 非流式：整段文本一次性发出。Phase 2 接入流式后，这里会变成多个增量。
            if reasoning := assistant.reasoning_text():
                yield stamp(ReasoningDelta(text=reasoning))
            if text := assistant.text():
                yield stamp(TextDelta(text=text))

            tool_uses = assistant.tool_uses()

            if not tool_uses:
                cost, currency = self._estimate_cost(accumulated)
                yield stamp(
                    RunCompleted(
                        text=assistant.text(),
                        steps=step,
                        usage=accumulated,
                        cost=cost,
                        currency=currency,
                        duration_ms=_elapsed_ms(started_at),
                    )
                )
                return

            results: list[ToolResultBlock] = []
            for use in tool_uses:
                yield stamp(
                    ToolCallStarted(
                        tool_use_id=use.id,
                        tool_name=use.name,
                        arguments=use.input,
                    )
                )

                tool_started = time.perf_counter()
                block = await self._execute(use, ctx)
                duration = _elapsed_ms(tool_started)
                results.append(block)

                yield stamp(
                    ToolResultEvent(
                        tool_use_id=use.id,
                        tool_name=use.name,
                        content=block.content,
                        is_error=block.is_error,
                        duration_ms=duration,
                    )
                )

            messages.append(Message.from_tool_results(results))

        last_text = messages[-1].text() if messages else ""
        yield stamp(
            RunFailed(
                error_type="MaxStepsExceeded",
                message=(
                    f"达到最大步数 {self.max_steps} 仍未给出最终答复。"
                    f"最后一次模型输出：{last_text[:200]}"
                ),
                steps=self.max_steps,
            )
        )

    # ------------------------------------------------------------ 内部

    async def _execute(self, use, ctx: ToolContext) -> ToolResultBlock:
        """执行一个 tool_use，返回结果块。

        模型给出坏 JSON 是一种特殊的失败：它连参数都没能正确序列化，
        所以走单独的分支，把原文回灌让它重来。
        """
        if use.raw_arguments is not None:
            return ToolResultBlock(
                tool_use_id=use.id,
                content=_BAD_JSON_TEMPLATE.format(
                    name=use.name, raw=use.raw_arguments[:800]
                ),
                is_error=True,
            )

        return await self.tools.invoke(use.name, use.input, ctx, tool_use_id=use.id)

    def _generation_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs

    def _estimate_cost(self, usage: Usage) -> tuple[float | None, str]:
        """估算花费。价格表里查不到这个模型就返回 ``None``，不编数字。"""
        pricing = pricing_for(self.model.name)
        if pricing is None:
            return None, ""
        return estimate_cost(usage, pricing), pricing.currency


async def collect(stream: AsyncIterator[RunEvent]) -> RunResult:
    """消费整条事件流，汇聚成 :class:`RunResult`。"""
    result = RunResult(run_id="")

    async for event in stream:
        result.run_id = event.run_id

        match event.type:
            case "run_completed":
                result.text = event.text
                result.steps = event.steps
                result.usage = event.usage
                result.cost = event.cost
                result.currency = event.currency
                result.duration_ms = event.duration_ms
            case "run_failed":
                result.error = event.message
                result.error_type = event.error_type
                result.steps = event.steps
            case "run_cancelled":
                result.cancelled = True
                result.steps = event.steps

    return result


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
