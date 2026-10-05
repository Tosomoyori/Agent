"""评测框架：判据、指标、运行器。

这些测试评的是**评测框架自己**——一份判据写错的评测比没有评测更糟，
因为它会给出一个看起来可信的错误结论。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit.core.usage import Usage
from agentkit.evaluation import (
    CaseResult,
    CaseRun,
    CheckSpec,
    Comparison,
    EvalCase,
    EvalRunner,
    ReplayModel,
    ScriptStep,
    SuiteResult,
    load_cases,
    run_checks,
    write_report,
)
from agentkit.evaluation.metrics import percentile
from agentkit.runtime.agent import Agent
from agentkit.tools.builtin import register_builtin_tools


def _run(**overrides) -> CaseRun:
    base = CaseRun(answer="答案是 42", workspace=None)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# ================================================================ 判据


class TestAnswerChecks:
    def test_contains_passes(self):
        outcomes = run_checks(_run(), [CheckSpec(type="answer_contains", contains=["42"])])
        assert outcomes[0].passed

    def test_contains_fails_and_names_what_is_missing(self):
        outcomes = run_checks(_run(), [CheckSpec(type="answer_contains", contains=["99"])])
        assert not outcomes[0].passed
        assert "99" in outcomes[0].detail

    def test_not_contains(self):
        outcomes = run_checks(
            _run(), [CheckSpec(type="answer_not_contains", contains=["42"])]
        )
        assert not outcomes[0].passed


class TestFileChecks:
    def test_file_exists(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace),
            [CheckSpec(type="file_exists", path="hello.txt")],
        )
        assert outcomes[0].passed

    def test_file_exists_reports_a_directory_as_failure(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace), [CheckSpec(type="file_exists", path="sub")]
        )
        assert not outcomes[0].passed
        assert "目录" in outcomes[0].detail

    def test_file_missing(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace), [CheckSpec(type="file_missing", path="nope.txt")]
        )
        assert outcomes[0].passed

    def test_file_contains(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace),
            [CheckSpec(type="file_contains", path="hello.txt", contains=["第一行", "第三行"])],
        )
        assert outcomes[0].passed

    def test_file_contains_shows_a_preview_on_failure(self, workspace: Path):
        """失败时要能一眼看出是「文件没建」还是「建了但内容不对」。"""
        outcomes = run_checks(
            _run(workspace=workspace),
            [CheckSpec(type="file_contains", path="hello.txt", contains=["不存在的词"])],
        )
        assert not outcomes[0].passed
        assert "实际内容开头" in outcomes[0].detail

    def test_file_not_contains(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace),
            [CheckSpec(type="file_not_contains", path="hello.txt", contains=["第四行"])],
        )
        assert outcomes[0].passed

    def test_file_not_contains_trivially_passes_for_missing_file(self, workspace: Path):
        outcomes = run_checks(
            _run(workspace=workspace),
            [CheckSpec(type="file_not_contains", path="nope.txt", contains=["x"])],
        )
        assert outcomes[0].passed

    def test_dir_exists(self, workspace: Path):
        assert run_checks(
            _run(workspace=workspace), [CheckSpec(type="dir_exists", path="sub")]
        )[0].passed

    def test_checks_without_a_workspace_fail_loudly(self):
        """没工作区时不该静默通过——那会让整条判据变成摆设。"""
        outcomes = run_checks(_run(workspace=None), [CheckSpec(type="file_exists", path="x")])
        assert not outcomes[0].passed
        assert "工作区" in outcomes[0].detail


class TestToolChecks:
    def test_tools_called_all_semantics(self):
        run = _run(tools_called=[("read_file", {}), ("write_file", {})])
        both = CheckSpec(type="tools_called", tools=["read_file", "write_file"])
        assert run_checks(run, [both])[0].passed
        assert not run_checks(
            run, [CheckSpec(type="tools_called", tools=["read_file", "run_command"])]
        )[0].passed

    def test_tools_called_any_semantics(self):
        """多工具的 ``tools_called`` 有歧义，必须由用例显式声明匹配方式。"""
        run = _run(tools_called=[("read_file", {})])
        any_spec = CheckSpec(
            type="tools_called", tools=["read_file", "search_in_file"], match="any"
        )
        assert run_checks(run, [any_spec])[0].passed

        all_spec = CheckSpec(type="tools_called", tools=["read_file", "search_in_file"])
        assert not run_checks(run, [all_spec])[0].passed

    def test_tools_not_called(self):
        run = _run(tools_called=[("read_file", {})])
        assert run_checks(run, [CheckSpec(type="tools_not_called", tools=["write_file"])])[0].passed
        assert not run_checks(
            run, [CheckSpec(type="tools_not_called", tools=["read_file"])]
        )[0].passed

    def test_tool_args_include_is_stricter_than_tool_name_alone(self):
        """选对工具但参数写错是最常见的失败模式，只看工具名会全部放过。"""
        run = _run(tools_called=[("read_file", {"file_path": "wrong.txt"})])
        spec = CheckSpec(
            type="tool_args_include", tools=["read_file"], args={"file_path": "right.txt"}
        )
        assert not run_checks(run, [spec])[0].passed

    def test_tool_args_include_matches_subset(self):
        run = _run(tools_called=[("read_file", {"file_path": "a.txt", "extra": 1})])
        spec = CheckSpec(type="tool_args_include", tools=["read_file"], args={"file_path": "a.txt"})
        assert run_checks(run, [spec])[0].passed

    def test_no_failed_tools(self):
        assert run_checks(_run(), [CheckSpec(type="no_failed_tools")])[0].passed
        assert not run_checks(
            _run(failed_tools=["read_file"]), [CheckSpec(type="no_failed_tools")]
        )[0].passed

    def test_max_steps(self):
        assert run_checks(_run(steps=2), [CheckSpec(type="max_steps", limit=3)])[0].passed
        assert not run_checks(_run(steps=5), [CheckSpec(type="max_steps", limit=3)])[0].passed


class TestCheckRobustness:
    def test_case_without_checks_fails(self):
        """没有判据的用例视为失败——那说明用例写漏了，不是「默认通过」。"""
        outcomes = run_checks(_run(), [])
        assert len(outcomes) == 1 and not outcomes[0].passed
        assert "没有定义任何判据" in outcomes[0].detail

    def test_a_broken_check_does_not_crash_the_run(self, workspace: Path):
        """判据自己出错不该让整轮评测挂掉。"""
        spec = CheckSpec(type="file_contains", path="hello.txt", contains=[])
        spec.path = None  # 制造一个会让 checker 抛异常的状态
        outcomes = run_checks(_run(workspace=workspace), [spec])
        assert len(outcomes) == 1


# ================================================================ 指标


def _result(case_id: str, trial: int, passed: bool, **run_kwargs) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        trial=trial,
        passed=passed,
        run=CaseRun(
            steps=run_kwargs.get("steps", 2),
            usage=run_kwargs.get("usage", Usage(input_tokens=100, output_tokens=20)),
            cost=run_kwargs.get("cost", 0.001),
            duration_ms=run_kwargs.get("duration_ms", 1000),
        ),
    )


class TestSuiteMetrics:
    def test_task_success_rate(self):
        result = SuiteResult(
            suite="s", trials=1,
            results=[_result("a", 1, True), _result("b", 1, False)],
        )
        assert result.task_success_rate() == 0.5

    def test_empty_suite_is_zero_not_an_error(self):
        assert SuiteResult(suite="s", trials=1).task_success_rate() == 0.0
        assert SuiteResult(suite="s", trials=1).pass_pow_k() == 0.0

    def test_pass_at_k_versus_pass_pow_k(self):
        """这是整套指标里最容易被混淆的一对，用同一个数据集把它们分开。"""
        results = [
            # a：3 次里只成功了 1 次 —— 有潜力，但不可靠
            _result("a", 1, True), _result("a", 2, False), _result("a", 3, False),
            # b：3 次全成功 —— 可靠
            _result("b", 1, True), _result("b", 2, True), _result("b", 3, True),
        ]
        suite = SuiteResult(suite="s", trials=3, results=results)

        assert suite.pass_at_k() == 1.0                      # 两个用例都至少成功过一次
        assert suite.pass_pow_k() == pytest.approx(0.5)      # 只有 b 是 3 次全成
        assert suite.task_success_rate() == pytest.approx(4 / 6)

        # 不等式恒成立
        assert suite.pass_pow_k() <= suite.task_success_rate() <= suite.pass_at_k()

    def test_pass_pow_k_refuses_partial_trial_data(self):
        """跑了 2 次都过，不能冒充「跑 5 次都过」。"""
        suite = SuiteResult(
            suite="s", trials=5,
            results=[_result("a", 1, True), _result("a", 2, True)],
        )
        assert suite.pass_pow_k() == 0.0

    def test_weighted_success_rate(self):
        cases = [
            EvalCase(id="a", prompt="x", weight=3.0),
            EvalCase(id="b", prompt="y", weight=1.0),
        ]
        suite = SuiteResult(
            suite="s", trials=1, cases=cases,
            results=[_result("a", 1, True), _result("b", 1, False)],
        )
        assert suite.weighted_task_success_rate() == pytest.approx(0.75)

    def test_total_cost_is_none_when_any_run_lacks_a_price(self):
        """3 次里 2 次查到价格就不能当完整数据报出去——那样的数字看着精确，实际少算。"""
        suite = SuiteResult(
            suite="s", trials=1,
            results=[
                _result("a", 1, True, cost=0.001),
                _result("b", 1, True, cost=None),
            ],
        )
        assert suite.total_cost() is None
        assert suite.cost_per_success() is None

    def test_cost_per_success(self):
        suite = SuiteResult(
            suite="s", trials=1,
            results=[
                _result("a", 1, True, cost=0.002),
                _result("b", 1, False, cost=0.002),
            ],
        )
        assert suite.total_cost() == pytest.approx(0.004)
        assert suite.cost_per_success() == pytest.approx(0.004)

    def test_tool_accuracy_is_none_without_tool_checks(self):
        """没有工具类判据时返回 None，而不是编一个 1.0。"""
        suite = SuiteResult(suite="s", trials=1, results=[_result("a", 1, True)])
        assert suite.tool_call_accuracy() is None

    def test_tool_accuracy_only_counts_tool_checks(self):
        from agentkit.evaluation import CheckOutcome

        results = [
            CaseResult(
                case_id="a", trial=1, passed=False,
                outcomes=[
                    CheckOutcome(
                        spec=CheckSpec(type="tools_called", tools=["f"]), passed=True
                    ),
                    CheckOutcome(
                        spec=CheckSpec(type="answer_contains", contains=["x"]), passed=False
                    ),
                ],
                run=CaseRun(),
            )
        ]
        suite = SuiteResult(suite="s", trials=1, results=results)
        # 只有工具类判据参与，所以是 1.0 而不是 0.5
        assert suite.tool_call_accuracy() == 1.0

    def test_latency_percentiles(self):
        suite = SuiteResult(
            suite="s", trials=1,
            results=[
                _result(str(i), 1, True, duration_ms=d)
                for i, d in enumerate([100, 200, 300, 400, 500])
            ],
        )
        assert suite.latency_p50() == pytest.approx(300)
        assert suite.latency_p95() == pytest.approx(480)

    def test_average_steps_ignores_zero(self):
        suite = SuiteResult(
            suite="s", trials=1,
            results=[_result("a", 1, True, steps=4), _result("b", 1, True, steps=0)],
        )
        assert suite.average_steps() == pytest.approx(4.0)

    def test_average_tool_calls(self):
        suite = SuiteResult(
            suite="s", trials=1,
            results=[
                CaseResult(
                    case_id="a", trial=1, passed=True,
                    run=CaseRun(tools_called=[("f", {})]),
                ),
                CaseResult(case_id="b", trial=1, passed=True, run=CaseRun(tools_called=[])),
            ],
        )
        assert suite.average_tool_calls() == pytest.approx(0.5)

    def test_failing_checks_tally(self):
        from agentkit.evaluation import CheckOutcome

        suite = SuiteResult(
            suite="s", trials=1,
            results=[
                CaseResult(
                    case_id="a", trial=1, passed=False,
                    outcomes=[
                        CheckOutcome(spec=CheckSpec(type="file_exists", path="x"), passed=False),
                        CheckOutcome(spec=CheckSpec(type="answer_contains"), passed=False),
                    ],
                    run=CaseRun(),
                )
            ],
        )
        assert suite.failing_checks() == {"file_exists": 1, "answer_contains": 1}

    def test_percentile_edge_cases(self):
        assert percentile([], 0.5) == 0.0
        assert percentile([7.0], 0.9) == 7.0


class TestComparison:
    def test_deltas(self):
        base = SuiteResult(suite="base", trials=1, results=[_result("a", 1, False)])
        cand = SuiteResult(suite="cand", trials=1, results=[_result("a", 1, True)])
        deltas = Comparison(baseline=base, candidate=cand).deltas()
        assert deltas["task_success_rate"]["delta"] == pytest.approx(1.0)

    def test_render_mentions_direction(self):
        base = SuiteResult(suite="base", trials=1, results=[_result("a", 1, False)])
        cand = SuiteResult(suite="cand", trials=1, results=[_result("a", 1, True)])
        text = Comparison(baseline=base, candidate=cand).render()
        assert "task_success_rate" in text
        assert "base" in text and "cand" in text


# ================================================================ 用例加载


class TestCaseLoading:
    def test_load_from_file(self, tmp_path: Path):
        path = tmp_path / "suite.jsonl"
        path.write_text(
            json.dumps({"id": "a", "prompt": "问题"}) + "\n"
            + json.dumps({"id": "b", "prompt": "问题2"}) + "\n",
            encoding="utf-8",
        )
        cases = load_cases(path)
        assert [c.id for c in cases] == ["a", "b"]

    def test_load_from_directory(self, tmp_path: Path):
        (tmp_path / "one.jsonl").write_text('{"id": "a", "prompt": "x"}\n', encoding="utf-8")
        (tmp_path / "two.jsonl").write_text('{"id": "b", "prompt": "y"}\n', encoding="utf-8")
        assert len(load_cases(tmp_path)) == 2

    def test_blank_lines_and_comments_are_skipped(self, tmp_path: Path):
        path = tmp_path / "suite.jsonl"
        path.write_text(
            '// 这是注释\n\n{"id": "a", "prompt": "x"}\n\n', encoding="utf-8"
        )
        assert len(load_cases(path)) == 1

    def test_duplicate_ids_are_rejected(self, tmp_path: Path):
        """id 重复会让报告里的数字对不上号。"""
        path = tmp_path / "suite.jsonl"
        path.write_text(
            '{"id": "a", "prompt": "x"}\n{"id": "a", "prompt": "y"}\n', encoding="utf-8"
        )
        with pytest.raises(ValueError, match="id 重复"):
            load_cases(path)

    def test_bad_line_reports_its_number(self, tmp_path: Path):
        path = tmp_path / "suite.jsonl"
        path.write_text('{"id": "a", "prompt": "x"}\n{不是合法 JSON}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="第 2 行"):
            load_cases(path)


# ================================================================ 运行器


class TestReplayModel:
    async def test_plays_back_a_script(self, workspace: Path):
        model = ReplayModel(
            [
                ScriptStep(call=("read_file", {"file_path": "hello.txt"})),
                ScriptStep(answer="完成了"),
            ]
        )
        agent = Agent(
            model=model,
            tools=register_builtin_tools(groups=["fs"]),
            workspace=workspace,
            temperature=None,
        )
        result = await agent.run("读文件")
        assert result.ok
        assert result.text == "完成了"
        assert result.steps == 2

    async def test_exhausted_script_yields_a_plain_answer(self):
        """脚本耗尽时给个平淡的答复，而不是抛异常。

        抛异常会让评测因为「框架的边界处理」失败，而不是因为 agent 的表现——
        判据本来就会发现该调的工具没调。
        """
        model = ReplayModel([])
        from agentkit.llm.base import accumulate

        completion = await accumulate(model.stream(messages=[]))
        assert "耗尽" in completion.message.text()


class TestRunner:
    async def test_each_trial_gets_a_fresh_workspace(self, tmp_path: Path):
        """**这是 pass^k 有意义的前提。**

        如果 k 次试验共用一个目录，第 1 次已经把目标文件建好了，第 2 次什么都不做
        也能「通过」——pass^k 会虚高得毫无意义。
        """
        seen: list[Path] = []

        def factory(workspace: Path, case: EvalCase):
            seen.append(workspace)
            return Agent(
                model=ReplayModel(list(case.script)),
                tools=register_builtin_tools(groups=["fs"]),
                workspace=workspace,
                temperature=None,
            )

        case = EvalCase(
            id="write_it",
            prompt="创建 out.txt",
            script=[ScriptStep(call=("write_file", {"file_path": "out.txt", "content": "x"}))],
            checks=[CheckSpec(type="file_exists", path="out.txt")],
        )

        runner = EvalRunner(factory, trials=3, work_root=tmp_path, mode="hermetic")
        result = await runner.run([case], suite_name="s")

        assert result.total_runs == 3
        assert len(set(seen)) == 3, "每次试验必须用不同的工作区"
        assert result.pass_pow_k() == 1.0

    async def test_setup_files_are_written_per_trial(self, tmp_path: Path):
        def factory(workspace: Path, case: EvalCase):
            return Agent(
                model=ReplayModel(list(case.script)),
                tools=register_builtin_tools(groups=["fs"]),
                workspace=workspace,
                temperature=None,
            )

        case = EvalCase(
            id="modify",
            prompt="改文件",
            setup_files={"target.txt": "旧内容\n"},
            script=[ScriptStep(answer="不改")],
            checks=[CheckSpec(type="file_contains", path="target.txt", contains=["旧内容"])],
        )

        runner = EvalRunner(factory, trials=2, work_root=tmp_path, mode="hermetic")
        result = await runner.run([case], suite_name="s")
        # 每次试验都重新写入了 setup 文件，所以两次都能通过
        assert result.task_success_rate() == 1.0

    async def test_failing_case_is_reported_with_details(self, tmp_path: Path):
        def factory(workspace: Path, case: EvalCase):
            return Agent(
                model=ReplayModel(list(case.script)),
                tools=register_builtin_tools(groups=["fs"]),
                workspace=workspace,
                temperature=None,
            )

        case = EvalCase(
            id="should_fail",
            prompt="创建 never.txt",
            script=[ScriptStep(answer="我建好了")],  # 实际上什么都没建
            checks=[CheckSpec(type="file_exists", path="never.txt")],
        )

        runner = EvalRunner(factory, trials=1, work_root=tmp_path, mode="hermetic")
        result = await runner.run([case], suite_name="s")

        assert result.task_success_rate() == 0.0
        failures = result.results[0].failures
        assert len(failures) == 1
        assert "不存在" in failures[0].detail

    async def test_runner_survives_a_crashing_factory(self, tmp_path: Path):
        """一次试验崩了不该让整套评测挂掉。"""
        def factory(workspace: Path, case: EvalCase):
            raise RuntimeError("工厂炸了")

        case = EvalCase(id="x", prompt="x", checks=[CheckSpec(type="no_failed_tools")])
        runner = EvalRunner(factory, trials=1, work_root=tmp_path, mode="hermetic")
        result = await runner.run([case], suite_name="s")

        assert result.results[0].error is not None
        assert "RuntimeError" in result.results[0].error
        assert not result.results[0].passed

    async def test_trials_must_be_positive(self, tmp_path: Path):
        with pytest.raises(ValueError, match="trials 至少是 1"):
            EvalRunner(lambda w, c: None, trials=0)

    async def test_cost_comes_from_events(self, tmp_path: Path):
        def factory(workspace: Path, case: EvalCase):
            return Agent(
                model=ReplayModel(list(case.script)),
                tools=register_builtin_tools(groups=[]),
                workspace=workspace,
                temperature=None,
            )

        case = EvalCase(
            id="plain",
            prompt="x",
            script=[ScriptStep(answer="好")],
            checks=[CheckSpec(type="answer_contains", contains=["好"])],
        )
        runner = EvalRunner(factory, trials=1, work_root=tmp_path, mode="hermetic")
        result = await runner.run([case], suite_name="s")

        assert result.results[0].run.usage.input_tokens > 0


# ================================================================ 报告


class TestReport:
    def test_markdown_contains_the_key_metrics(self):
        suite = SuiteResult(
            suite="demo", trials=2,
            results=[_result("a", 1, True), _result("a", 2, True)],
        )
        text = __import__(
            "agentkit.evaluation", fromlist=["render_markdown"]
        ).render_markdown(suite)
        assert "任务成功率" in text
        assert "pass^2" in text
        assert "pass@2" in text

    def test_report_files_are_written(self, tmp_path: Path):
        suite = SuiteResult(suite="demo", trials=1, results=[_result("a", 1, True)])
        md_path, json_path = write_report(suite, tmp_path)
        assert md_path.exists() and json_path.exists()

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["suite"] == "demo"
        assert payload["metrics"]["task_success_rate"] == 1.0

    def test_report_lists_failures(self):
        from agentkit.evaluation import CheckOutcome, render_markdown

        suite = SuiteResult(
            suite="demo", trials=1,
            results=[
                CaseResult(
                    case_id="broken", trial=1, passed=False,
                    outcomes=[
                        CheckOutcome(
                            spec=CheckSpec(type="file_exists", path="missing.txt"),
                            passed=False,
                            detail="文件不存在",
                        )
                    ],
                    run=CaseRun(tools_called=[("read_file", {})]),
                )
            ],
        )
        text = render_markdown(suite)
        assert "失败明细" in text
        assert "broken" in text
        assert "文件不存在" in text
        assert "read_file" in text  # 实际调用了什么，便于归因
