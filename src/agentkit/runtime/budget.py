"""run 级资源预算。

四类上限，各自的失败模式不同：

* **步数**：防止「模型一直调工具不收敛」——旧的 ``MAX_STEPS`` 常量只覆盖了这一类。
* **token**：上下文和成本的主要来源。单看步数没用，一步里塞进十万 token 也一样爆。
* **成本**：与 token 不是线性关系（缓存命中按 1/10 计费），所以必须单独设限。
* **时长**：外部依赖卡住时的兜底。

**为什么是「检查后抛错」而不是「超了就截断」**：截断会让模型收到一个不完整的
上下文，它可能据此给出一个看起来很确定的错误答案。宁可整个 run 失败并说明原因，
也不要产出一个自己都不知道是错的答复。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..core.errors import BudgetExceeded
from ..core.usage import Usage

__all__ = ["Budget", "BudgetTracker"]


@dataclass(frozen=True)
class Budget:
    """一次 run 的资源上限。``None`` 表示该维度不设限。"""

    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    max_duration_s: float | None = None
    currency: str = "CNY"

    def describe(self) -> str:
        parts = []
        if self.max_steps is not None:
            parts.append(f"{self.max_steps} 步")
        if self.max_tokens is not None:
            parts.append(f"{self.max_tokens:,} tokens")
        if self.max_cost is not None:
            parts.append(f"{self.max_cost:.4f} {self.currency}")
        if self.max_duration_s is not None:
            parts.append(f"{self.max_duration_s:g} 秒")
        return "、".join(parts) or "不限"


@dataclass
class _Consumption:
    """实际消耗。"""

    steps: int = 0
    usage: Usage = Usage()
    cost: float | None = None
    elapsed_s: float = 0.0


class BudgetTracker:
    """累计消耗并在越界时抛 :class:`BudgetExceeded`。

    调用方在每个可能显著消耗资源的节点后调 :meth:`check`——每步结束时、
    每次模型调用后、每次工具执行后。
    """

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.consumption = _Consumption()
        self._started_at = time.perf_counter()

    # ------------------------------------------------------------ 记账

    def tick(self) -> None:
        """记一步。"""
        self.consumption.steps += 1
        self._refresh_elapsed()

    def add_usage(self, usage: Usage) -> None:
        self.consumption.usage = self.consumption.usage + usage
        self._refresh_elapsed()

    def set_cost(self, cost: float | None) -> None:
        """记录按当前用量估算的花费。

        由引擎在每次模型调用后写入——成本要按**累计**用量算，因为缓存命中的
        价格不是线性的，逐次累加会算错。
        """
        self.consumption.cost = cost
        self._refresh_elapsed()

    def _refresh_elapsed(self) -> None:
        self.consumption.elapsed_s = time.perf_counter() - self._started_at

    # ------------------------------------------------------------ 判定

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._started_at

    @property
    def remaining_steps(self) -> int | None:
        if self.budget.max_steps is None:
            return None
        return max(0, self.budget.max_steps - self.consumption.steps)

    @property
    def remaining_tokens(self) -> int | None:
        if self.budget.max_tokens is None:
            return None
        return max(0, self.budget.max_tokens - self.consumption.usage.total_tokens)

    @property
    def remaining_cost(self) -> float | None:
        if self.budget.max_cost is None or self.consumption.cost is None:
            return None
        return max(0.0, self.budget.max_cost - self.consumption.cost)

    def check(self) -> None:
        """越界时抛 :class:`BudgetExceeded`。"""
        self._refresh_elapsed()
        b, c = self.budget, self.consumption

        # 步数单独判定：它由引擎的循环条件保证，这里只是兜底
        if b.max_steps is not None and c.steps > b.max_steps:
            raise BudgetExceeded(
                f"超出步数预算：已用 {c.steps} 步，上限 {b.max_steps} 步"
            )

        if b.max_tokens is not None and c.usage.total_tokens > b.max_tokens:
            raise BudgetExceeded(
                f"超出 token 预算：已用 {c.usage.total_tokens:,}，上限 {b.max_tokens:,}"
            )

        if b.max_cost is not None and c.cost is not None and c.cost > b.max_cost:
            raise BudgetExceeded(
                f"超出成本预算：已用 {c.cost:.4f} {b.currency}，上限 {b.max_cost:.4f}"
            )

        if b.max_duration_s is not None and c.elapsed_s > b.max_duration_s:
            raise BudgetExceeded(
                f"超出时长预算：已用 {c.elapsed_s:.1f} 秒，上限 {b.max_duration_s:g} 秒"
            )

    def summary(self) -> str:
        """一行摘要，用于收尾事件与 CLI 输出。"""
        c = self.consumption
        parts = [f"{c.steps} 步", f"{c.elapsed_s * 1000:.0f}ms"]
        parts.append(
            f"入 {c.usage.input_tokens:,} / 出 {c.usage.output_tokens:,} tokens"
        )
        if c.usage.cached_input_tokens:
            parts.append(f"缓存命中 {c.usage.cached_input_tokens:,}")
        if c.cost is not None:
            parts.append(f"约 {c.cost:.4f} {self.budget.currency}")
        return "  ".join(parts)
