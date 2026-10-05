"""评测运行器。

## 每个 trial 一个独立工作区

这不是洁癖，是正确性的前提。如果 k 次试验共用一个目录：第 1 次已经把目标文件
建好了，第 2 次只需要什么都不做就能「通过」——pass^k 会虚高得毫无意义。

所以每次试验都在 ``<root>/<case_id>/trial_<n>/`` 里跑，用例声明的
``setup_files`` 每次重新写入。

## 并发

默认并发 3。不是怕机器扛不住，是怕 **API 限流**：并发太高会让一批试验因为 429
失败，那些失败是**环境造成的**，不是 agent 造成的，混进成功率里就是脏数据。
宁可慢一点。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..core.events import RunEvent
from ..core.usage import Usage
from ..runtime.agent import Agent
from .case import EvalCase
from .checkers import CaseRun, run_checks
from .metrics import CaseResult, SuiteResult

__all__ = ["EvalRunner", "AgentFactory"]

#: 给定工作区，造一个 agent。两种模式的差别全在这个工厂里。
AgentFactory = Callable[[Path, EvalCase], Agent | Awaitable[Agent]]


class EvalRunner:
    """跑一个评测套件。"""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        trials: int = 1,
        max_concurrency: int = 3,
        work_root: Path | None = None,
        mode: str = "live",
        keep_workspaces: bool = False,
    ) -> None:
        if trials < 1:
            raise ValueError("trials 至少是 1")
        self.agent_factory = agent_factory
        self.trials = trials
        self.max_concurrency = max(1, max_concurrency)
        self.work_root = work_root
        self.mode = mode
        self.keep_workspaces = keep_workspaces

    # ------------------------------------------------------------ 主入口

    async def run(
        self,
        cases: list[EvalCase],
        *,
        suite_name: str = "suite",
        on_result: Callable[[CaseResult], None] | None = None,
    ) -> SuiteResult:
        started = time.perf_counter()
        result = SuiteResult(
            suite=suite_name,
            trials=self.trials,
            cases=list(cases),
            mode=self.mode,
        )

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def guarded(case: EvalCase, trial: int) -> CaseResult:
            async with semaphore:
                outcome = await self._run_one(case, trial)
            if on_result is not None:
                on_result(outcome)
            return outcome

        tasks = [
            guarded(case, trial)
            for case in cases
            for trial in range(1, self.trials + 1)
        ]
        result.results = await asyncio.gather(*tasks)

        result.duration_s = time.perf_counter() - started
        if self.work_root is not None and not self.keep_workspaces:
            shutil.rmtree(self.work_root, ignore_errors=True)
        return result

    # ------------------------------------------------------------ 单次试验

    async def _run_one(self, case: EvalCase, trial: int) -> CaseResult:
        workspace = self._prepare_workspace(case, trial)
        run_trace = CaseRun(workspace=workspace)
        error: str | None = None

        try:
            agent = await _maybe_await(self.agent_factory(workspace, case))
            try:
                await self._drive(agent, case, run_trace)
            finally:
                await agent.aclose()
        except Exception as exc:  # noqa: BLE001 - 一次试验崩了不该让整套评测挂掉
            error = f"{type(exc).__name__}: {exc}"

        outcomes = run_checks(run_trace, case.checks)
        return CaseResult(
            case_id=case.id,
            trial=trial,
            passed=error is None and all(o.passed for o in outcomes),
            outcomes=outcomes,
            run=run_trace,
            error=error,
        )

    async def _drive(self, agent: Agent, case: EvalCase, trace: CaseRun) -> None:
        """跑一次，把事件流里的信息收进 :class:`CaseRun`。"""
        streamed: list[str] = []

        async for event in agent.stream(case.prompt):
            _collect(event, trace, streamed)

        # 答案优先用 run_completed 里的完整文本；缺失时退回流式增量拼出来的。
        # （被取消或失败时不会有 run_completed，那种情况本来就判不过。）
        if not trace.answer:
            trace.answer = "".join(streamed)

    def _prepare_workspace(self, case: EvalCase, trial: int) -> Path:
        root = self.work_root or Path.cwd() / ".agentkit" / "eval"
        workspace = root / case.id / f"trial_{trial}"
        # 先清空：上一轮跑剩下的文件会让这一次的判据失真
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)

        for relative, content in case.setup_files.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")

        return workspace


def _collect(event: RunEvent, trace: CaseRun, streamed: list[str]) -> None:
    """把事件里的信息累积到 CaseRun。"""
    match event.type:
        case "text_delta":
            streamed.append(event.text)
        case "tool_call_started":
            trace.tools_called.append((event.tool_name, dict(event.arguments)))
        case "tool_result":
            if event.is_error:
                trace.failed_tools.append(event.tool_name)
        case "run_completed":
            trace.answer = event.text
            trace.steps = event.steps
            trace.usage = event.usage
            trace.cost = event.cost
            trace.duration_ms = event.duration_ms
        case "run_failed":
            trace.steps = event.steps
            trace.error = f"{event.error_type}: {event.message}"
        case "run_cancelled":
            trace.steps = event.steps
            trace.error = f"被取消: {event.reason}"
        case "usage_reported":
            # 失败路径下没有 run_completed，用最后一条累计用量兜底，
            # 免得成本统计把出错的试验当成「零成本」
            trace.usage = event.cumulative


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


def summarize_usage(results: list[CaseResult]) -> Usage:
    total = Usage()
    for result in results:
        total = total + result.run.usage
    return total
