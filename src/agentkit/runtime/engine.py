"""推理引擎——原生 tool calling 的循环。

循环本身只有四步，且**没有任何文本解析**：

1. 把消息历史发给模型（附带工具 schema），流式收回来；
2. 模型要么给最终答复，要么给出一个或多个 ``tool_use``；
3. 有 ``tool_use`` 就执行，把结果打包成一条 user 消息追加回去；
4. 回到第 1 步。

对比旧实现：那里每一轮都要用正则从模型输出里抠 ```json``` 块、``json.loads``、
失败再用 ``ast.literal_eval`` 兜底、再失败就把原始输出当成 observation 塞回去。
整条路径有四个静默降级点，每个都会让模型的错误被吞掉。现在这些全消失了。

引擎**只产事件**，不返回值。事件经 :class:`~agentkit.runtime.bus.EventBus` 汇聚，
详见那个模块的说明。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import AgentKitError, BudgetExceeded, RunCancelled
from ..core.events import (
    ApprovalRequested,
    ApprovalResolved,
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
    RunCancelled as RunCancelledEvent,
)
from ..core.events import (
    ToolResult as ToolResultEvent,
)
from ..core.ids import new_run_id
from ..core.types import Message, ToolResultBlock, validate_conversation
from ..core.usage import ZERO_USAGE, Usage, estimate_cost, pricing_for
from ..llm.base import ChatModel
from ..memory.manager import MemoryManager
from ..tools.base import ToolContext
from ..tools.policy import ApprovalDecision, ApprovalRequest, Approver
from ..tools.registry import ToolRegistry
from .budget import Budget, BudgetTracker
from .bus import EventBus
from .cancellation import CancellationToken, race_cancellation
from .prompts import render_system_prompt

__all__ = ["Engine", "RunResult", "collect"]

logger = logging.getLogger(__name__)

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
    #: 完整的推理轨迹（assistant/user 消息交替），可用于续跑或事后分析。
    messages: list[Message] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


class _NotifyingApprover:
    """把一个审批通道包一层，把请求与决定都发成事件。

    这是审批能在 SSE 上工作的关键一环：``ApprovalRequested`` 在**工具仍然阻塞
    等待**的时候就被推进总线，客户端因此有机会看到请求并回应。
    """

    def __init__(self, inner: Approver, bus: EventBus) -> None:
        self._inner = inner
        self._bus = bus

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self._bus.emit(
            ApprovalRequested(
                tool_use_id=request.tool_use_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                reason=request.reason,
            )
        )
        decision = await self._inner.request(request)
        self._bus.emit(
            ApprovalResolved(
                tool_use_id=request.tool_use_id,
                approved=decision.approved,
                note=decision.note,
            )
        )
        return decision


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
        budget: Budget | None = None,
        memory: MemoryManager | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.workspace = Path(workspace).resolve()
        self.name = name
        self.max_steps = max_steps
        self.temperature = temperature
        self.max_tokens = max_tokens
        #: 记忆管理器。为 ``None`` 时不落盘、不做上下文裁剪。
        self.memory = memory
        # max_steps 由循环自己管（它要给出 MaxStepsExceeded 这个更具体的错误）。
        # 预算对象只负责 token / 成本 / 时长，避免同一个上限被两处判定、
        # 报出来的错误类型却不一样。
        self.budget = budget or Budget()

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
        cancellation: CancellationToken | None = None,
        approver: Approver | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        """跑一次完整的推理循环，逐个 yield 事件。

        :param session_id: 会话 id。给了且配了记忆管理器时，历史从存储里取、
            本轮结果会落盘。
        """
        resolved_run_id = run_id or new_run_id()
        bus = EventBus(resolved_run_id)

        producer = asyncio.create_task(
            self._drive(
                bus,
                user_input,
                history=history,
                cancellation=cancellation,
                approver=approver,
                session_id=session_id,
            )
        )

        try:
            async for event in bus.drain():
                yield event
        finally:
            # 消费者提前退出（HTTP 连接断开、调用方 break）时要把生产者停掉，
            # 否则那个 run 会继续在后台烧 token。
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer

    # ------------------------------------------------------------ 生产端

    async def _drive(
        self,
        bus: EventBus,
        user_input: str,
        *,
        history: Sequence[Message] | None,
        cancellation: CancellationToken | None,
        approver: Approver | None,
        session_id: str | None,
    ) -> None:
        """跑循环并把事件推进总线。任何异常都会转成终止事件。"""
        try:
            await self._loop(
                bus,
                user_input,
                history=history,
                cancellation=cancellation,
                approver=approver,
                session_id=session_id,
            )
        except RunCancelled as exc:
            bus.emit(RunCancelledEvent(reason=str(exc)))
        except BudgetExceeded as exc:
            bus.emit(RunFailed(error_type="BudgetExceeded", message=str(exc)))
        except AgentKitError as exc:
            bus.emit(RunFailed(error_type=type(exc).__name__, message=str(exc)))
        except asyncio.CancelledError:
            bus.emit(RunCancelledEvent(reason="调用方终止了这个 run"))
            raise
        except Exception as exc:  # noqa: BLE001 - 运行时不能让调用方炸
            # 意料之外的错误：照实报出去，但把栈写进日志——事件里只放摘要，
            # 否则一条 traceback 会把 SSE 的载荷撑得没法看。
            logger.exception("run %s 出现未预期的异常", bus.run_id)
            bus.emit(
                RunFailed(
                    error_type=type(exc).__name__,
                    message=f"未预期的内部错误: {exc}",
                )
            )
        finally:
            bus.close()

    async def _loop(
        self,
        bus: EventBus,
        user_input: str,
        *,
        history: Sequence[Message] | None,
        cancellation: CancellationToken | None,
        approver: Approver | None,
        session_id: str | None,
    ) -> None:
        started_at = time.perf_counter()
        token = cancellation or CancellationToken()
        tracker = BudgetTracker(self.budget)

        ctx = ToolContext(
            workspace=self.workspace,
            run_id=bus.run_id,
            approver=_NotifyingApprover(approver, bus) if approver else None,
        )

        messages = await self._build_context(user_input, history, session_id)
        turn_start = len(messages) - 1  # 本轮新增消息的起点（user 消息的下标）

        bus.emit(RunStarted(agent=self.name, model=self.model.name, input=user_input))

        schemas = self.tools.schemas() if len(self.tools) else None
        generation = self._generation_kwargs()
        accumulated = ZERO_USAGE

        while True:
            token.raise_if_cancelled()
            steps_done = tracker.consumption.steps
            if self.max_steps is not None and steps_done >= self.max_steps:
                # 达到步数上限时消息序列仍然是完整的（最后一步的工具结果已经配对），
                # 把这轮的部分进展记下来，下一轮接着用
                await self._persist(
                    session_id, messages[turn_start:], run_id=bus.run_id
                )
                bus.emit(
                    RunFailed(
                        error_type="MaxStepsExceeded",
                        message=(
                            f"达到最大步数 {self.max_steps} 仍未给出最终答复。"
                            f"最后一次模型输出：{_last_text(messages)[:200]}"
                        ),
                        steps=steps_done,
                    )
                )
                return

            step = steps_done + 1
            tracker.tick()
            bus.emit(StepStarted(step=step))

            # 每一轮都核对配对不变量。开销可以忽略，但能在产生孤儿 tool_result
            # 的第一时间定位到是哪一步写坏的，而不是等 API 报一个难懂的 400。
            validate_conversation(messages)

            completion = await self._call_model(
                bus, messages, schemas, generation, token, tracker
            )

            accumulated = accumulated + completion.usage
            tracker.add_usage(completion.usage)
            cost, currency = self._estimate_cost(accumulated)
            tracker.set_cost(cost)

            bus.emit(
                UsageReported(
                    step=step, usage=completion.usage, cumulative=accumulated
                )
            )

            assistant = completion.message
            messages.append(assistant)
            tracker.check()

            tool_uses = assistant.tool_uses()
            if not tool_uses:
                await self._persist(session_id, messages[turn_start:], run_id=bus.run_id)
                bus.emit(
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
                token.raise_if_cancelled()
                bus.emit(
                    ToolCallStarted(
                        tool_use_id=use.id,
                        tool_name=use.name,
                        arguments=use.input,
                    )
                )

                tool_started = time.perf_counter()
                block = await self._execute(use, ctx, token)
                results.append(block)

                bus.emit(
                    ToolResultEvent(
                        tool_use_id=use.id,
                        tool_name=use.name,
                        content=block.content,
                        is_error=block.is_error,
                        duration_ms=_elapsed_ms(tool_started),
                    )
                )
                tracker.check()

            messages.append(Message.from_tool_results(results))

    async def _call_model(
        self,
        bus: EventBus,
        messages: list[Message],
        schemas: list[Any] | None,
        generation: dict[str, Any],
        token: CancellationToken,
        tracker: BudgetTracker,
    ):
        """调一次模型，把流式增量转成事件，返回汇聚后的结果。

        用流式而不是一次性请求，是为了让正文和思维链能**边生成边推**。实测
        ``deepseek-flash`` 在回答"数到五"时产生了 1012 个思维链 token，
        不流式的话用户要盯着空白界面等好几秒。
        """
        from ..llm.base import StreamAccumulator

        accumulator = StreamAccumulator()

        async def consume() -> None:
            async for chunk in self.model.stream(
                messages=messages, tools=schemas, **generation
            ):
                token.raise_if_cancelled()
                accumulator.feed(chunk)
                if chunk.reasoning:
                    bus.emit(ReasoningDelta(text=chunk.reasoning))
                if chunk.text:
                    bus.emit(TextDelta(text=chunk.text))

        await race_cancellation(consume(), token)

        from ..llm.base import Completion

        return Completion(
            message=accumulator.message(),
            finish_reason=accumulator.finish_reason or "stop",
            usage=accumulator.usage,
        )

    async def _execute(
        self,
        use: Any,
        ctx: ToolContext,
        token: CancellationToken,
    ) -> ToolResultBlock:
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

        # race_cancellation 让取消能立刻生效：工具可能正在等子进程跑完，
        # 没有它就得等工具自己结束才发现没人要结果了。
        return await race_cancellation(
            self.tools.invoke(use.name, use.input, ctx, tool_use_id=use.id), token
        )

    # ------------------------------------------------------------ 上下文与持久化

    async def _build_context(
        self,
        user_input: str,
        history: Sequence[Message] | None,
        session_id: str | None,
    ) -> list[Message]:
        """装配这一轮要发给模型的消息序列。

        配了记忆管理器时，装配权交给它（它是唯一的 owner）；否则就地拼一份。
        两条路径产出的形状是一样的：``[system, ...历史, user]``。
        """
        if self.memory is not None:
            window = await self.memory.build_context(
                session_id,
                user_input,
                system=self.system_prompt,
                extra_history=history,
            )
            return list(window.messages)

        messages: list[Message] = []
        if self.system_prompt:
            messages.append(Message.system(self.system_prompt))
        messages.extend(history or [])
        messages.append(Message.user(user_input))
        return messages

    async def _persist(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        run_id: str,
    ) -> None:
        """把本轮消息落盘。失败不致命——记不上历史总好过整个 run 失败。

        **落盘前先校验配对不变量。** 取消或出错时消息序列可能是残缺的
        （assistant 的 ``tool_use`` 还没等到 ``tool_result``），存下去会让下一轮
        一开场就违反不变量、被 API 拒绝。宁可少记一次历史，也不要写进去一份
        下次读出来就炸的数据。
        """
        if self.memory is None or session_id is None or not messages:
            return

        try:
            validate_conversation(messages)
        except Exception:  # noqa: BLE001 - 具体是 InvalidConversation，但这里只关心"不合法"
            logger.info(
                "会话 %s 的本轮消息配对不完整（可能被取消或中断），跳过落盘", session_id
            )
            return

        try:
            await self.memory.record(session_id, messages, run_id=run_id)
        except Exception:  # noqa: BLE001 - 存储问题不该毁掉已经跑完的 run
            logger.exception("会话 %s 的消息落盘失败", session_id)

    # ------------------------------------------------------------ 辅助

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


def _last_text(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if text := message.text():
            return text
    return ""


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
