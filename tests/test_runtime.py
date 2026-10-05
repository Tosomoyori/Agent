"""取消传播、预算、审批通道。"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from agentkit.core.errors import BudgetExceeded, RunCancelled
from agentkit.core.usage import Usage
from agentkit.runtime.approval import (
    AutoApprover,
    CallbackApprover,
    ConsoleApprover,
    DenyApprover,
    QueueApprover,
)
from agentkit.runtime.budget import Budget, BudgetTracker
from agentkit.runtime.cancellation import CancellationToken, race_cancellation
from agentkit.runtime.engine import Engine
from agentkit.tools.base import ToolSpec
from agentkit.tools.policy import ApprovalDecision, ApprovalRequest
from agentkit.tools.registry import ToolRegistry

from .conftest import ScriptedModel, assistant_text, assistant_tools


def _engine(model, workspace, registry, **kwargs) -> Engine:
    return Engine(model, registry, workspace=workspace, **kwargs)


async def _drain(engine, prompt: str, **kwargs):
    return [event async for event in engine.run(prompt, **kwargs)]


def _types(events) -> list[str]:
    return [e.type for e in events]


# ================================================================ 取消


class TestCancellationToken:
    def test_starts_active(self):
        token = CancellationToken()
        assert not token.cancelled
        token.raise_if_cancelled()  # 不抛异常

    def test_cancel_sets_state_and_raises(self):
        token = CancellationToken()
        token.cancel("用户点了取消")
        assert token.cancelled
        with pytest.raises(RunCancelled, match="用户点了取消"):
            token.raise_if_cancelled()

    def test_cancel_is_idempotent_and_keeps_first_reason(self):
        token = CancellationToken()
        token.cancel("第一次")
        token.cancel("第二次")
        assert token.reason == "第一次"

    async def test_wait_returns_when_cancelled(self):
        token = CancellationToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)
        assert not waiter.done()
        token.cancel()
        await asyncio.wait_for(waiter, timeout=1)


class TestRaceCancellation:
    async def test_returns_result_when_not_cancelled(self):
        async def work():
            return 42

        assert await race_cancellation(work(), CancellationToken()) == 42

    async def test_raises_when_already_cancelled(self):
        token = CancellationToken()
        token.cancel("提前取消")

        async def work():  # pragma: no cover - 不该被执行
            raise AssertionError("被取消的 run 不该再跑任何工作")

        with pytest.raises(RunCancelled):
            await race_cancellation(work(), token)

    async def test_abandons_in_flight_work(self):
        """取消要能中断**正在跑**的协程，而不是等它自己结束。"""
        started = asyncio.Event()
        finished = False

        async def slow():
            nonlocal finished
            started.set()
            await asyncio.sleep(10)
            finished = True

        token = CancellationToken()
        task = asyncio.create_task(race_cancellation(slow(), token))
        await started.wait()
        token.cancel("不等了")

        with pytest.raises(RunCancelled):
            await asyncio.wait_for(task, timeout=2)
        assert not finished


class TestEngineCancellation:
    async def test_cancel_stops_the_loop_with_a_terminal_event(self, workspace):
        """取消必须以 ``run_cancelled`` 收尾——它是正常终止，不是异常。"""
        registry = ToolRegistry()

        async def slow(ctx, x: str = "") -> str:
            """慢工具。"""
            await asyncio.sleep(10)
            return "不会到这里"

        registry.register(slow)
        model = ScriptedModel(assistant_tools(("slow", {})))

        token = CancellationToken()
        asyncio.get_running_loop().call_later(0.05, token.cancel, "测试取消")

        events = await asyncio.wait_for(
            _drain(
                _engine(model, workspace, registry),
                "跑个慢工具",
                cancellation=token,
            ),
            timeout=5,
        )

        assert _types(events)[-1] == "run_cancelled"
        assert events[-1].reason == "测试取消"

    async def test_later_tools_do_not_run_after_cancel(self, workspace):
        """取消之后同一轮里后面的工具**不再执行**。

        这条是取消机制的核心价值：没有它，一个正在跑 15 步的 run 收到取消后
        还会把所有工具跑完、把 token 烧完。
        """
        executed: list[str] = []
        registry = ToolRegistry()

        async def slow(ctx, x: str = "") -> str:
            """会在中途被取消的工具。"""
            executed.append("slow:start")
            await asyncio.sleep(10)
            executed.append("slow:end")
            return "done"

        async def follower(ctx, x: str = "") -> str:
            """排在后面的工具。"""
            executed.append("follower")
            return "done"

        registry.register(slow)
        registry.register(follower)

        model = ScriptedModel(assistant_tools(("slow", {}), ("follower", {})))
        token = CancellationToken()
        asyncio.get_running_loop().call_later(0.05, token.cancel, "取消")

        events = await asyncio.wait_for(
            _drain(_engine(model, workspace, registry), "跑", cancellation=token),
            timeout=5,
        )

        assert _types(events)[-1] == "run_cancelled"
        assert "slow:start" in executed
        assert "slow:end" not in executed, "被取消的工具应当被中断，而不是跑完"
        assert "follower" not in executed, "取消之后不该再执行后续工具"

    async def test_model_is_not_called_again_after_cancel(self, workspace):
        """取消之后不该再发起模型调用——那一轮的钱白花了。

        这里用一个**自己取消 run** 的工具来制造确定性的时序，而不是用
        ``call_later`` 比拼速度：后者会随机器负载时快时慢，是个迟早要修的脆弱测试。
        """
        registry = ToolRegistry()
        token = CancellationToken()

        async def canceller(ctx, x: str = "") -> str:
            """在工具内部取消整个 run。"""
            token.cancel("工具里取消")
            return "ok"

        registry.register(canceller)
        model = ScriptedModel(
            assistant_tools(("canceller", {})),
            assistant_text("不该被调用到"),
        )

        events = await asyncio.wait_for(
            _drain(_engine(model, workspace, registry), "跑", cancellation=token),
            timeout=5,
        )

        assert _types(events)[-1] == "run_cancelled"
        assert len(model.calls) == 1, "取消后不该再调用模型"


# ================================================================ 预算


class TestBudgetTracker:
    def test_unlimited_by_default(self):
        tracker = BudgetTracker(Budget())
        for _ in range(100):
            tracker.tick()
        tracker.check()  # 不抛异常

    def test_step_limit(self):
        tracker = BudgetTracker(Budget(max_steps=2))
        tracker.tick()
        tracker.tick()
        tracker.check()
        tracker.tick()
        with pytest.raises(BudgetExceeded, match="步数预算"):
            tracker.check()

    def test_token_limit(self):
        tracker = BudgetTracker(Budget(max_tokens=100))
        tracker.add_usage(Usage(input_tokens=50, output_tokens=30))
        tracker.check()
        tracker.add_usage(Usage(input_tokens=30))
        with pytest.raises(BudgetExceeded, match="token 预算"):
            tracker.check()

    def test_cost_limit(self):
        tracker = BudgetTracker(Budget(max_cost=0.01))
        tracker.set_cost(0.005)
        tracker.check()
        tracker.set_cost(0.02)
        with pytest.raises(BudgetExceeded, match="成本预算"):
            tracker.check()

    def test_duration_limit(self):
        tracker = BudgetTracker(Budget(max_duration_s=0.0))
        with pytest.raises(BudgetExceeded, match="时长预算"):
            tracker.check()

    def test_cost_unknown_does_not_trigger(self):
        """查不到价格时不编造成本，因此也不该因为成本而失败。"""
        tracker = BudgetTracker(Budget(max_cost=0.001))
        tracker.set_cost(None)
        tracker.check()

    def test_remaining_calculations(self):
        tracker = BudgetTracker(Budget(max_steps=5, max_tokens=100, max_cost=1.0))
        tracker.tick()
        tracker.add_usage(Usage(input_tokens=40))
        tracker.set_cost(0.25)
        assert tracker.remaining_steps == 4
        assert tracker.remaining_tokens == 60
        assert tracker.remaining_cost == pytest.approx(0.75)

    def test_summary_mentions_key_numbers(self):
        tracker = BudgetTracker(Budget(max_steps=5))
        tracker.tick()
        tracker.add_usage(Usage(input_tokens=100, output_tokens=20))
        tracker.set_cost(0.01)
        text = tracker.summary()
        assert "1 步" in text
        assert "100" in text and "20" in text  # 入/出 token
        assert "0.0100" in text


class TestEngineBudget:
    async def test_token_budget_stops_the_run(self, workspace):
        registry = ToolRegistry()

        async def noop(ctx, x: str = "") -> str:
            """无操作。"""
            return "ok"

        registry.register(noop)

        # 每次调用报 5000 token，预算 8000 → 第二次调用后必然越界
        model = ScriptedModel(
            assistant_tools(("noop", {})),
            assistant_tools(("noop", {})),
            assistant_text("不该到这里"),
            usage=Usage(input_tokens=5000, output_tokens=0),
        )
        engine = Engine(model, registry, workspace=workspace, budget=Budget(max_tokens=8000))

        events = await _drain(engine, "跑")
        assert events[-1].type == "run_failed"
        assert events[-1].error_type == "BudgetExceeded"
        assert "token 预算" in events[-1].message


# ================================================================ 审批


def _request(tool_use_id: str = "c1") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="run_command",
        arguments={"command": "rm old.txt"},
        reason="命令包含写操作",
        tool_use_id=tool_use_id,
        command="rm old.txt",
    )


class TestApprovers:
    async def test_auto_approver_allows(self):
        assert (await AutoApprover().request(_request())).approved

    async def test_deny_approver_denies(self):
        """服务化场景的默认值——没人看着就别放行。"""
        decision = await DenyApprover("没有通道").request(_request())
        assert not decision.approved
        assert "没有通道" in decision.note

    async def test_callback_approver_with_bool(self):
        approver = CallbackApprover(lambda request: request.tool_name == "run_command")
        assert (await approver.request(_request())).approved

    async def test_callback_approver_with_async_callable(self):
        async def decide(request: ApprovalRequest) -> ApprovalDecision:
            return ApprovalDecision.deny("异步拒绝")

        decision = await CallbackApprover(decide).request(_request())
        assert not decision.approved and decision.note == "异步拒绝"

    async def test_console_approver_accepts_yes(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert (await ConsoleApprover().request(_request())).approved

    async def test_console_approver_defaults_to_no(self, monkeypatch):
        """直接回车是**拒绝**——大写 N 是约定的默认值，别让手滑变成批准。"""
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        assert not (await ConsoleApprover().request(_request())).approved

    async def test_console_approver_times_out(self, monkeypatch):
        def slow_input(prompt: str = "") -> str:
            import time

            time.sleep(2)
            return "y"

        monkeypatch.setattr("builtins.input", slow_input)
        decision = await ConsoleApprover(timeout=0.05).request(_request())
        assert not decision.approved and "超时" in decision.note


class TestQueueApprover:
    async def test_waits_for_an_external_decision(self):
        """SSE 场景的核心机制：外部（HTTP 路由）异步做出决定。"""
        approver = QueueApprover()
        task = asyncio.create_task(approver.request(_request("c1")))

        # 等到请求登记上来
        for _ in range(50):
            if approver.pending_ids:
                break
            await asyncio.sleep(0)
        assert approver.pending_ids == ["c1"]

        assert approver.resolve("c1", True, "用户批准") is True
        decision = await asyncio.wait_for(task, timeout=1)
        assert decision.approved and decision.note == "用户批准"

    async def test_resolving_an_unknown_id_returns_false(self):
        approver = QueueApprover()
        assert approver.resolve("不存在", True) is False

    async def test_can_deny(self):
        approver = QueueApprover()
        task = asyncio.create_task(approver.request(_request("c2")))
        for _ in range(50):
            if approver.pending_ids:
                break
            await asyncio.sleep(0)
        approver.resolve("c2", ApprovalDecision.deny("不行"))
        assert not (await asyncio.wait_for(task, timeout=1)).approved

    async def test_pending_entry_is_cleaned_up(self):
        approver = QueueApprover()
        task = asyncio.create_task(approver.request(_request("c3")))
        for _ in range(50):
            if approver.pending_ids:
                break
            await asyncio.sleep(0)
        approver.resolve("c3", True)
        await asyncio.wait_for(task, timeout=1)
        assert approver.pending_ids == []


class TestEngineApproval:
    async def test_approval_events_are_emitted(self, workspace):
        """审批的请求与决定都要发成事件——客户端靠它知道有人在等它回应。"""
        registry = ToolRegistry()

        async def dangerous(ctx, x: str = "") -> str:
            """需要审批的动作。"""
            approved = await ctx.request_approval(_approval_request(ctx))
            return "已批准" if approved else "被拒绝"

        registry.register(dangerous, dangerous=True)
        model = ScriptedModel(
            assistant_tools(("dangerous", {})),
            assistant_text("完成"),
        )

        events = await _drain(
            _engine(model, workspace, registry), "跑", approver=AutoApprover()
        )
        types = _types(events)

        assert "approval_requested" in types
        assert "approval_resolved" in types
        # 请求必须排在决定之前
        assert types.index("approval_requested") < types.index("approval_resolved")

        resolved = next(e for e in events if e.type == "approval_resolved")
        assert resolved.approved is True

    async def test_request_event_arrives_while_the_tool_is_still_blocked(
        self, workspace
    ):
        """**审批请求必须在工具仍然阻塞时就送达客户端。**

        这是事件总线存在的全部理由：如果事件只能在 await 返回之后才 yield，
        审批就成了「先等出结果再问你要不要批准」，逻辑反了。
        """
        registry = ToolRegistry()

        async def gated(ctx, x: str = "") -> str:
            """等审批的动作。"""
            approved = await ctx.request_approval(_approval_request(ctx))
            return "批准了" if approved else "拒绝了"

        registry.register(gated, dangerous=True)
        model = ScriptedModel(assistant_tools(("gated", {})), assistant_text("完成"))

        approver = QueueApprover()
        engine = _engine(model, workspace, registry)

        seen_request = asyncio.Event()
        finished = asyncio.Event()

        async def consume():
            async for event in engine.run("跑", approver=approver):
                if event.type == "approval_requested":
                    seen_request.set()
                    # 关键断言：此刻工具还没跑完（事件流还没结束）
                    assert not finished.is_set()
                    approver.resolve(event.tool_use_id, True, "批准")
                if event.type in ("run_completed", "run_failed", "run_cancelled"):
                    finished.set()

        await asyncio.wait_for(consume(), timeout=5)
        assert seen_request.is_set()

    async def test_denied_approval_becomes_an_error_result(self, workspace):
        registry = ToolRegistry()

        async def gated(ctx, x: str = "") -> str:
            """等审批的动作。"""
            from agentkit.core.errors import ApprovalDenied

            if not await ctx.request_approval(_approval_request(ctx)):
                raise ApprovalDenied("没批准")
            return "批准了"

        registry.register(gated, dangerous=True)
        model = ScriptedModel(assistant_tools(("gated", {})), assistant_text("完成"))

        events = await _drain(
            _engine(model, workspace, registry), "跑", approver=DenyApprover()
        )
        result = next(e for e in events if e.type == "tool_result")
        assert result.is_error
        assert events[-1].type == "run_completed"


def _approval_request(ctx) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="dangerous",
        arguments={},
        reason="测试用",
        tool_use_id=ctx.tool_use_id,
    )


# ================================================================ 工具执行细节


class TestToolContextPropagation:
    async def test_tool_use_id_is_visible_in_the_context(self, workspace):
        """审批靠 tool_use_id 关联请求与决定，所以它必须传得到工具里。"""
        seen: list[str] = []
        registry = ToolRegistry()

        async def spy(ctx, x: Annotated[str, "参数"] = "") -> str:
            """记录上下文里的 id。"""
            seen.append(ctx.tool_use_id)
            return "ok"

        registry.register(spy)
        model = ScriptedModel(assistant_tools(("spy", {}), id_prefix="boot"), assistant_text("好"))
        await _drain(_engine(model, workspace, registry), "跑")

        assert seen == ["boot_0"]

    async def test_state_is_shared_across_calls(self, workspace):
        """state 字典是同一个引用——工具之间要能共享进程内状态。"""
        registry = ToolRegistry()

        async def writer(ctx, x: str = "") -> str:
            """写状态。"""
            ctx.state["counter"] = ctx.state.get("counter", 0) + 1
            return "ok"

        async def reader(ctx, x: str = "") -> str:
            """读状态。"""
            return str(ctx.state.get("counter", "没读到"))

        registry.register(writer)
        registry.register(reader)
        model = ScriptedModel(
            assistant_tools(("writer", {}), ("reader", {})),
            assistant_text("完成"),
        )
        events = await _drain(_engine(model, workspace, registry), "跑")

        results = [e.content for e in events if e.type == "tool_result"]
        assert results == ["ok", "1"]


class _Unused:
    """占位，避免 linter 抱怨 import 未使用。"""

    _ = ToolSpec


class TestApprovalCorrelation:
    async def test_tool_use_id_is_filled_in_automatically(self, workspace):
        """工具未填写 tool_use_id 时，由上下文补齐。

        该字段缺失时，服务端会使用一个客户端无法获知的兜底键挂起审批，
        run 随之静默停滞。因此该补齐逻辑是必要路径，而非边界情况的防御。
        """
        registry = ToolRegistry()

        async def forgetful(ctx, x: Annotated[str, "参数"] = "") -> str:
            """刻意不传 tool_use_id。"""
            from agentkit.tools.policy import ApprovalRequest

            request = ApprovalRequest(tool_name="forgetful", arguments={}, reason="测试")
            assert not request.tool_use_id
            approved = await ctx.request_approval(request)
            return f"tool_use_id={request.tool_use_id!r} approved={approved}"

        registry.register(forgetful, dangerous=True)
        model = ScriptedModel(
            assistant_tools(("forgetful", {}), id_prefix="boot"),
            assistant_text("好"),
        )

        events = await _drain(
            _engine(model, workspace, registry), "跑", approver=AutoApprover()
        )
        result = next(e for e in events if e.type == "tool_result")
        assert "tool_use_id='boot_0'" in result.content


class TestQueueApproverTimeout:
    async def test_waits_then_denies_on_timeout(self):
        """没人接手的 run 不该永远挂着——从外部看不出它是「在等人」还是「卡死了」。"""
        approver = QueueApprover(timeout=0.05)
        decision = await approver.request(_request("c9"))
        assert not decision.approved
        assert "超时" in decision.note

    async def test_pending_entry_is_cleaned_up_after_timeout(self):
        approver = QueueApprover(timeout=0.05)
        await approver.request(_request("c9"))
        assert approver.pending_ids == []

    async def test_timeout_can_be_disabled(self):
        approver = QueueApprover(timeout=None)
        task = asyncio.create_task(approver.request(_request("c1")))
        for _ in range(50):
            if approver.pending_ids:
                break
            await asyncio.sleep(0)
        assert approver.pending_ids == ["c1"]
        approver.resolve("c1", True)
        assert (await asyncio.wait_for(task, timeout=1)).approved
