"""评测：数据集、判据、指标、运行器、报告。

设计要点见各模块的文档，其中最重要的两条：

* **评的是执行后的状态**，不是对话记录（:mod:`agentkit.evaluation.case`）；
* **pass^k 才是上线的关键指标**，不是 TSR 或 pass@k
  （:mod:`agentkit.evaluation.metrics`）。
"""

from .case import CheckSpec, EvalCase, ScriptStep, dump_cases, load_cases
from .checkers import CHECKERS, CaseRun, CheckOutcome, run_checks
from .metrics import CaseResult, Comparison, SuiteResult, percentile
from .replay import ReplayModel, build_from_script
from .report import render_markdown, write_report
from .runner import EvalRunner

__all__ = [
    "CHECKERS",
    "CaseResult",
    "CaseRun",
    "CheckOutcome",
    "CheckSpec",
    "Comparison",
    "EvalCase",
    "EvalRunner",
    "ReplayModel",
    "ScriptStep",
    "SuiteResult",
    "build_from_script",
    "dump_cases",
    "load_cases",
    "percentile",
    "render_markdown",
    "run_checks",
    "write_report",
]
