"""判据的执行。

每条判据接收一个 :class:`CaseRun`（一次运行的全部痕迹）和工作区路径，
返回 ``(是否通过, 说明)``。

判据的**说明文字是给人看的**——失败时报告里要能一眼看出是「文件没建」还是
「建了但内容不对」，而不只是「失败了」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.usage import Usage
from .case import CheckSpec

__all__ = ["CaseRun", "CheckOutcome", "run_checks", "CHECKERS"]


@dataclass
class CaseRun:
    """一次运行留下的全部痕迹。判据只看这里面的东西。"""

    answer: str = ""
    tools_called: list[tuple[str, dict]] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    duration_ms: int = 0
    error: str | None = None
    workspace: Path | None = None

    @property
    def tool_names(self) -> list[str]:
        return [name for name, _ in self.tools_called]


@dataclass
class CheckOutcome:
    """一条判据的结果。"""

    spec: CheckSpec
    passed: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "通过" if self.passed else "失败"
        label = self.spec.note or _default_label(self.spec)
        return f"{mark}: {label}" + (f" —— {self.detail}" if self.detail else "")


def _default_label(spec: CheckSpec) -> str:
    if spec.type.startswith("answer"):
        return f"答复{'包含' if 'not' not in spec.type else '不含'} {spec.contains}"
    if spec.type.startswith("file") or spec.type.startswith("dir"):
        return f"{spec.type} {spec.path!r}"
    if spec.type in ("tools_called", "tools_not_called"):
        return f"{spec.type} {spec.tools}"
    if spec.type == "tool_args_include":
        return f"工具入参包含 {spec.args}"
    if spec.type == "no_failed_tools":
        return "没有失败的工具调用"
    if spec.type == "max_steps":
        return f"步数不超过 {spec.limit}"
    return spec.type


def _resolve(run: CaseRun, relative: str) -> Path | None:
    if run.workspace is None:
        return None
    return run.workspace / relative


def _check_answer_contains(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    missing = [text for text in spec.contains if text not in run.answer]
    if missing:
        return False, f"答复里缺少 {missing}"
    return True, ""


def _check_answer_not_contains(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    found = [text for text in spec.contains if text in run.answer]
    if found:
        return False, f"答复里不该出现 {found}"
    return True, ""


def _check_file_exists(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None:
        return False, "没有工作区"
    if not target.exists():
        return False, "文件不存在"
    if target.is_dir():
        return False, "路径存在但是个目录"
    return True, f"{target.stat().st_size} 字节"


def _check_file_missing(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None:
        return False, "没有工作区"
    if target.exists():
        return False, "文件不该存在，但它在了"
    return True, ""


def _check_file_contains(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None:
        return False, "没有工作区"
    if not target.is_file():
        return False, "文件不存在"
    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return False, f"读取失败: {exc}"

    missing = [needle for needle in spec.contains if needle not in text]
    if missing:
        preview = text[:200].replace("\n", "⏎")
        return False, f"文件里缺少 {missing}；实际内容开头: {preview!r}"
    return True, ""


def _check_file_not_contains(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None or not target.is_file():
        return True, "文件不存在，自然不含"
    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return True, ""
    found = [needle for needle in spec.contains if needle in text]
    if found:
        return False, f"文件里不该出现 {found}"
    return True, ""


def _check_dir_exists(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None:
        return False, "没有工作区"
    if not target.is_dir():
        return False, "目录不存在"
    return True, f"{len(list(target.iterdir()))} 项"


def _check_dir_missing(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    target = _resolve(run, spec.path or "")
    if target is None:
        return False, "没有工作区"
    if target.exists():
        return False, "目录不该存在"
    return True, ""


def _check_tools_called(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    """期望调用了某些工具。

    ``spec.match`` 决定「任意一个」还是「全部」——两种读法都说得通，所以
    由用例显式声明，不由这里猜。
    """
    called = set(run.tool_names)

    if spec.match == "any":
        if not (called & set(spec.tools)):
            return False, f"这些工具一个都没调：{spec.tools}；实际调用了 {run.tool_names}"
        return True, f"调用了 {sorted(called & set(spec.tools))}"

    missing = [name for name in spec.tools if name not in called]
    if missing:
        return False, f"没有调用 {missing}；实际调用了 {run.tool_names}"
    return True, f"调用了 {run.tool_names}"


def _check_tools_not_called(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    called = set(run.tool_names)
    forbidden = [name for name in spec.tools if name in called]
    if forbidden:
        return False, f"不该调用 {forbidden}"
    return True, ""


def _check_tool_args_include(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    """某个工具的入参要包含给定的键值。

    这条比 ``tools_called`` 严格一档：**选对了工具但参数写错**是最常见的失败模式，
    只看工具名会把这类错误全部放过。
    """
    for name, arguments in run.tools_called:
        if name not in spec.tools:
            continue
        if all(arguments.get(key) == value for key, value in spec.args.items()):
            return True, f"{name}{arguments}"
    observed = [(n, a) for n, a in run.tools_called if n in spec.tools]
    return False, f"没有一次 {spec.tools} 调用满足 {spec.args}；观察到 {observed}"


def _check_no_failed_tools(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    if run.failed_tools:
        return False, f"这些工具调用失败了: {run.failed_tools}"
    return True, ""


def _check_max_steps(run: CaseRun, spec: CheckSpec) -> tuple[bool, str]:
    limit = spec.limit if spec.limit is not None else 0
    if run.steps > limit:
        return False, f"用了 {run.steps} 步，上限 {limit}"
    return True, f"{run.steps} 步"


CHECKERS: dict[str, Callable[[CaseRun, CheckSpec], tuple[bool, str]]] = {
    "answer_contains": _check_answer_contains,
    "answer_not_contains": _check_answer_not_contains,
    "file_exists": _check_file_exists,
    "file_missing": _check_file_missing,
    "file_contains": _check_file_contains,
    "file_not_contains": _check_file_not_contains,
    "dir_exists": _check_dir_exists,
    "dir_missing": _check_dir_missing,
    "tools_called": _check_tools_called,
    "tools_not_called": _check_tools_not_called,
    "tool_args_include": _check_tool_args_include,
    "no_failed_tools": _check_no_failed_tools,
    "max_steps": _check_max_steps,
}


def run_checks(run: CaseRun, specs: list[CheckSpec]) -> list[CheckOutcome]:
    """跑一遍全部判据。没有判据时视为失败——那说明用例写漏了。"""
    if not specs:
        return [
            CheckOutcome(
                spec=CheckSpec(type="no_failed_tools"),
                passed=False,
                detail="用例没有定义任何判据，无法判定是否通过",
            )
        ]

    outcomes: list[CheckOutcome] = []
    for spec in specs:
        checker = CHECKERS.get(spec.type)
        if checker is None:  # pragma: no cover - Literal 已经限定了取值
            outcomes.append(
                CheckOutcome(spec=spec, passed=False, detail=f"未知判据 {spec.type!r}")
            )
            continue
        try:
            passed, detail = checker(run, spec)
        except Exception as exc:  # noqa: BLE001 - 判据自己出错不该让整轮评测崩掉
            passed, detail = False, f"判据执行异常: {type(exc).__name__}: {exc}"
        outcomes.append(CheckOutcome(spec=spec, passed=passed, detail=detail))

    return outcomes
