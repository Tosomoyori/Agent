"""评测指标。

## 三个成功率指标，别混用

设一次评测跑了 ``n`` 个用例、每个用例重复 ``k`` 次：

* **TSR（任务成功率）** = 全部 ``n×k`` 次里通过的比例。衡量**平均表现**。
* **pass@k** = 「k 次里至少成功一次」的用例比例。衡量**潜力**——给足机会能不能做出来。
* **pass^k** = 「k 次全部成功」的用例比例。衡量**可靠性**。

**上线的关键指标是 pass^k。** 一个 pass@5 = 1.0 但 pass^5 = 0.4 的 agent，
意味着它能做对，但五次里只有四成能五次全对——用户每次调用都在赌。τ-bench 的
原始结果里 retail 场景 pass^8 不到 25%，这个数字比 TSR 有信息量得多。

注意 ``pass^k ≤ TSR ≤ pass@k`` 恒成立，报告里三个都给，谁也别想挑好看的那个。

## 成本与延迟

成本必须和成功率**并列**看：一个成功率高一倍但成本高十倍的方案不一定是改进。
所以报告里给「每成功任务成本」，而不是只给总花费——总花费没有可比性。

没有比例关系的地方不编：成本查不到价格时返回 ``None``，不填 0 冒充。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..core.usage import Usage
from .case import CheckSpec, EvalCase
from .checkers import CaseRun, CheckOutcome

__all__ = ["CaseResult", "SuiteResult", "percentile"]


def percentile(values: list[float], fraction: float) -> float:
    """线性插值的分位数。空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass
class CaseResult:
    """一次试验的结果。"""

    case_id: str
    trial: int
    passed: bool
    outcomes: list[CheckOutcome] = field(default_factory=list)
    run: CaseRun = field(default_factory=CaseRun)
    #: 运行本身出错（比如 API 失败），区别于「判据没过」
    error: str | None = None

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed]


@dataclass
class SuiteResult:
    """一次套件评测的完整结果。"""

    suite: str
    trials: int
    cases: list[EvalCase] = field(default_factory=list)
    results: list[CaseResult] = field(default_factory=list)
    mode: str = "live"
    duration_s: float = 0.0

    # ------------------------------------------------------------ 分组

    def by_case(self) -> dict[str, list[CaseResult]]:
        grouped: dict[str, list[CaseResult]] = defaultdict(list)
        for result in self.results:
            grouped[result.case_id].append(result)
        return dict(grouped)

    @property
    def executed_cases(self) -> list[str]:
        return sorted(self.by_case())

    # ------------------------------------------------------------ 成功率

    @property
    def total_runs(self) -> int:
        return len(self.results)

    @property
    def passed_runs(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def task_success_rate(self) -> float:
        """全部试验里通过的比例。"""
        if not self.results:
            return 0.0
        return self.passed_runs / self.total_runs

    def pass_at_k(self) -> float:
        """至少成功一次的用例比例。"""
        grouped = self.by_case()
        if not grouped:
            return 0.0
        hit = sum(1 for rs in grouped.values() if any(r.passed for r in rs))
        return hit / len(grouped)

    def pass_pow_k(self, k: int | None = None) -> float:
        """k 次**全部**成功的用例比例——可靠性指标。

        ``k`` 缺省用本次跑的试验次数。少于 k 次的用例（比如中途被打断）
        按未通过计——不能用「跑了 2 次都过」冒充「跑 5 次都过」。
        """
        k = k or self.trials
        grouped = self.by_case()
        if not grouped:
            return 0.0
        perfect = sum(
            1
            for rs in grouped.values()
            if len(rs) >= k and all(r.passed for r in rs)
        )
        return perfect / len(grouped)

    def weighted_task_success_rate(self) -> float:
        """按用例权重加权的成功率。权重必须是正数，否则会被静默忽略。"""
        weights = {case.id: case.weight for case in self.cases}
        total = 0.0
        earned = 0.0
        for result in self.results:
            weight = weights.get(result.case_id, 1.0)
            if weight <= 0:
                continue
            total += weight
            if result.passed:
                earned += weight
        return earned / total if total else 0.0

    # ------------------------------------------------------------ 工具使用

    def tool_call_accuracy(self) -> float | None:
        """工具类判据的通过率。

        只统计和工具有关的判据（``tools_called`` / ``tool_args_include`` 等）——
        把「文件内容对不对」也算进「工具调用准不准」会得到一个谁也不知道
        在衡量什么的数字。没有工具类判据时返回 ``None``。

        **看这个指标要注意**：它比 TSR 宽松，因为一个用例里可能有多条工具判据，
        全对才贡献 1。它回答的是「该用哪个工具、参数怎么填」，不回答「任务完成没有」。
        """
        tool_types = {
            "tools_called",
            "tools_not_called",
            "tool_args_include",
            "no_failed_tools",
        }
        relevant = [
            outcome
            for result in self.results
            for outcome in result.outcomes
            if outcome.spec.type in tool_types
        ]
        if not relevant:
            return None
        return sum(1 for o in relevant if o.passed) / len(relevant)

    def tool_selection_accuracy(self) -> float | None:
        """只看「选对工具」的通过率（含参数判据），不含其他。"""
        relevant = [
            outcome
            for result in self.results
            for outcome in result.outcomes
            if outcome.spec.type in ("tools_called", "tool_args_include")
        ]
        if not relevant:
            return None
        return sum(1 for o in relevant if o.passed) / len(relevant)

    # ------------------------------------------------------------ 成本与延迟

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        for result in self.results:
            total = total + result.run.usage
        return total

    def total_cost(self) -> float | None:
        """总成本。只要有**任意一次**没拿到价格就返回 ``None``。

        不想让「3 次里 2 次查到价格」被当成完整数据报出去——那种数字看起来
        精确，实际少算了三分之一。
        """
        costs = [r.run.cost for r in self.results]
        if not costs or any(cost is None for cost in costs):
            return None
        return sum(costs)  # type: ignore[arg-type]

    def cost_per_success(self) -> float | None:
        total = self.total_cost()
        if total is None or not self.passed_runs:
            return None
        return total / self.passed_runs

    def latency_ms(self) -> list[float]:
        return [float(r.run.duration_ms) for r in self.results if r.run.duration_ms]

    def latency_p50(self) -> float:
        return percentile(self.latency_ms(), 0.5)

    def latency_p95(self) -> float:
        return percentile(self.latency_ms(), 0.95)

    def average_steps(self) -> float:
        steps = [r.run.steps for r in self.results if r.run.steps]
        return sum(steps) / len(steps) if steps else 0.0

    def average_tool_calls(self) -> float:
        counts = [len(r.run.tools_called) for r in self.results]
        return sum(counts) / len(counts) if counts else 0.0

    # ------------------------------------------------------------ 失败归因

    def error_runs(self) -> list[CaseResult]:
        """运行本身出错的（不是判据没过）。"""
        return [r for r in self.results if r.error]

    def failing_checks(self) -> dict[str, int]:
        """按判据类型统计失败次数，用来定位系统性问题。"""
        tally: dict[str, int] = defaultdict(int)
        for result in self.results:
            for outcome in result.failures:
                tally[outcome.spec.type] += 1
        return dict(sorted(tally.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict:
        return {
            "suite": self.suite,
            "mode": self.mode,
            "trials": self.trials,
            "cases": len(self.by_case()),
            "runs": self.total_runs,
            "duration_s": round(self.duration_s, 2),
            "metrics": {
                "task_success_rate": round(self.task_success_rate(), 4),
                "weighted_task_success_rate": round(
                    self.weighted_task_success_rate(), 4
                ),
                "pass_at_k": round(self.pass_at_k(), 4),
                "pass_pow_k": round(self.pass_pow_k(), 4),
                "tool_call_accuracy": _round(self.tool_call_accuracy()),
                "tool_selection_accuracy": _round(self.tool_selection_accuracy()),
                "average_steps": round(self.average_steps(), 2),
                "average_tool_calls": round(self.average_tool_calls(), 2),
                "latency_p50_ms": round(self.latency_p50(), 1),
                "latency_p95_ms": round(self.latency_p95(), 1),
                "total_cost": _round(self.total_cost(), 6),
                "cost_per_success": _round(self.cost_per_success(), 6),
                "total_input_tokens": self.total_usage.input_tokens,
                "total_output_tokens": self.total_usage.output_tokens,
            },
            "failing_checks": self.failing_checks(),
            "results": [
                {
                    "case_id": r.case_id,
                    "trial": r.trial,
                    "passed": r.passed,
                    "error": r.error,
                    "steps": r.run.steps,
                    "duration_ms": r.run.duration_ms,
                    "cost": r.run.cost,
                    "tools": r.run.tool_names,
                    "failures": [o.describe() for o in r.failures],
                }
                for r in self.results
            ],
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# ------------------------------------------------------------------ 对照


@dataclass
class Comparison:
    """两次评测的对照。

    只报差值也会误导——0.02 的提升在 n=5 的样本上没有意义。所以同时给出
    样本量，让人自己判断。
    """

    baseline: SuiteResult
    candidate: SuiteResult

    def deltas(self) -> dict[str, dict[str, float | None]]:
        base = self.baseline.to_dict()["metrics"]
        cand = self.candidate.to_dict()["metrics"]

        out: dict[str, dict[str, float | None]] = {}
        for key in base:
            before, after = base[key], cand.get(key)
            if before is None or after is None:
                out[key] = {"before": before, "after": after, "delta": None}
            else:
                out[key] = {
                    "before": before,
                    "after": after,
                    "delta": round(after - before, 6),
                }
        return out

    def render(self) -> str:
        lines = [
            f"基准: {self.baseline.suite}（{self.baseline.total_runs} 次运行）",
            f"对照: {self.candidate.suite}（{self.candidate.total_runs} 次运行）",
            "",
            f"{'指标':<32}{'基准':>12}{'对照':>12}{'变化':>12}",
            "-" * 68,
        ]
        for key, values in self.deltas().items():
            before = values["before"]
            after = values["after"]
            delta = values["delta"]
            arrow = ""
            if delta is not None and delta != 0:
                # 成本指标降低是好事，方向要反过来标
                good = delta < 0 if "cost" in key or "latency" in key else delta > 0
                arrow = " ↑" if good else " ↓"
            lines.append(
                f"{key:<32}{_fmt(before):>12}{_fmt(after):>12}"
                f"{_fmt(delta, signed=True) + arrow:>12}"
            )
        return "\n".join(lines)


def _fmt(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def specs_of(case: EvalCase, *types: str) -> list[CheckSpec]:
    """便捷函数：取出某个用例里指定类型的判据。"""
    return [spec for spec in case.checks if spec.type in types]
