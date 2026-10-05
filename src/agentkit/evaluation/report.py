"""报告输出。

Markdown 供人阅读，JSON 供程序消费。不生成 HTML 报告：该部分成本应投入指标定义
的明确性，而非可视化呈现。

报告保留**失败明细**与**样本量**。仅报告一个成功率数值是不充分的：
n=5 与 n=500 下的同一数值含义完全不同，而失败明细才是指出改进方向的依据。
"""

from __future__ import annotations

import json
from pathlib import Path

from .metrics import Comparison, SuiteResult

__all__ = ["render_markdown", "write_report", "report_paths"]


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def render_markdown(result: SuiteResult) -> str:
    """把结果渲染成 Markdown。"""
    grouped = result.by_case()
    metrics = result.to_dict()["metrics"]

    lines: list[str] = [
        f"# 评测报告：{result.suite}",
        "",
        f"- 模式：**{result.mode}**"
        + ("（假模型回放，不消耗 API）" if result.mode == "hermetic" else "（真实调用模型）"),
        f"- 用例数：{len(grouped)}　试验次数：每例 {result.trials} 次"
        f"　总运行：{result.total_runs}",
        f"- 耗时：{result.duration_s:.1f} 秒",
        "",
        "## 核心指标",
        "",
        "| 指标 | 值 | 说明 |",
        "| --- | --- | --- |",
        f"| 任务成功率 (TSR) | **{_pct(metrics['task_success_rate'])}** "
        f"| 全部 {result.total_runs} 次运行里通过的比例 |",
        f"| pass@{result.trials} | {_pct(metrics['pass_at_k'])} "
        f"| 至少成功一次的用例比例，衡量**潜力** |",
        f"| pass^{result.trials} | **{_pct(metrics['pass_pow_k'])}** "
        f"| {result.trials} 次全部成功的用例比例，衡量**可靠性** |",
        f"| 工具调用准确率 | {_pct(metrics['tool_call_accuracy'])} "
        f"| 工具类判据的通过率（选对工具 + 参数正确） |",
        f"| 平均步数 | {_num(metrics['average_steps'])} | 越少越好，但不该以牺牲正确性为代价 |",
        f"| 平均工具调用数 | {_num(metrics['average_tool_calls'])} | |",
        f"| 延迟 P50 / P95 | {_num(metrics['latency_p50_ms'], 0, ' ms')} / "
        f"{_num(metrics['latency_p95_ms'], 0, ' ms')} | P95 更能反映用户的实际体感 |",
        f"| 总成本 | {_num(metrics['total_cost'], 4)} | "
        f"{'查不到价格' if metrics['total_cost'] is None else '按内置价格表估算'} |",
        f"| 每次成功成本 | {_num(metrics['cost_per_success'], 4)} | "
        "成本要和成功率并列看，总花费没有可比性 |",
        f"| 总 token | 入 {metrics['total_input_tokens']:,} / "
        f"出 {metrics['total_output_tokens']:,} | |",
        "",
        "> `pass^k ≤ TSR ≤ pass@k` 恒成立。三个都列出来，免得只挑好看的那个报。",
        "",
    ]

    # ---- 逐用例 ----
    lines += [
        "## 逐用例结果",
        "",
        "| 用例 | 通过/试验 | 平均步数 | 失败判据 |",
        "| --- | --- | --- | --- |",
    ]
    for case_id in result.executed_cases:
        runs = grouped[case_id]
        passed = sum(1 for r in runs if r.passed)
        mark = "✅" if passed == len(runs) else ("⚠️" if passed else "❌")
        avg_steps = sum(r.run.steps for r in runs) / len(runs)
        failures: list[str] = []
        for run in runs:
            for outcome in run.failures:
                text = outcome.spec.note or outcome.spec.type
                if text not in failures:
                    failures.append(text)
        lines.append(
            f"| {mark} `{case_id}` | {passed}/{len(runs)} | {avg_steps:.1f} "
            f"| {'; '.join(failures) if failures else '—'} |"
        )
    lines.append("")

    # ---- 失败归因 ----
    failing = result.failing_checks()
    if failing:
        lines += ["## 失败归因", "", "| 判据类型 | 失败次数 |", "| --- | --- |"]
        for check_type, count in failing.items():
            lines.append(f"| `{check_type}` | {count} |")
        lines.append("")

    errors = result.error_runs()
    if errors:
        lines += [
            "## 运行错误",
            "",
            f"{len(errors)} 次运行本身出错了（不是判据没过）：",
            "",
            "| 用例 | 试验 | 错误 |",
            "| --- | --- | --- |",
        ]
        for run in errors[:20]:
            lines.append(f"| `{run.case_id}` | #{run.trial} | {run.error} |")
        lines.append("")

    # ---- 失败明细 ----
    detail_lines: list[str] = []
    for run in result.results:
        if run.passed:
            continue
        detail_lines.append(f"- **{run.case_id}** #{run.trial}")
        if run.error:
            detail_lines.append(f"  - 运行错误: {run.error}")
        for outcome in run.failures:
            label = outcome.spec.note or outcome.spec.type
            detail_lines.append(f"  - {label}：{outcome.detail}")
        if run.run.tools_called:
            calls = ", ".join(name for name, _ in run.run.tools_called)
            detail_lines.append(f"  - 实际调用了：{calls}")

    if detail_lines:
        lines += ["## 失败明细", "", *detail_lines, ""]

    return "\n".join(lines)


def report_paths(out_dir: Path, suite: str, mode: str) -> tuple[Path, Path]:
    stem = f"{suite.replace('/', '_')}_{mode}"
    return out_dir / f"{stem}.md", out_dir / f"{stem}.json"


def write_report(result: SuiteResult, out_dir: Path) -> tuple[Path, Path]:
    """写出 Markdown 与 JSON 两份报告，返回它们的路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, json_path = report_paths(out_dir, result.suite, result.mode)

    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md_path, json_path


def render_comparison(comparison: Comparison) -> str:  # pragma: no cover - 便捷函数
    return comparison.render()
