"""追踪：事件流 → span 树。

这些用例同时验证了两件事：追踪器本身的映射是对的，以及**事件序列确实足以还原
时间结构**。后者是「引擎只产事件、追踪器只消费」这个设计的隐含契约——
如果哪次改动让事件顺序变了，这里会先炸。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from agentkit.core.events import (
    RunCompleted,
    RunFailed,
    RunStarted,
    StepStarted,
    TextDelta,
    ToolCallStarted,
    ToolResult,
    UsageReported,
)
from agentkit.core.usage import Usage
from agentkit.observability import (
    ConsoleExporter,
    JsonlExporter,
    MemoryExporter,
    Observer,
    SpanKind,
    SpanStatus,
    TraceBuilder,
    provider_name_for,
    read_jsonl,
)
from agentkit.runtime.engine import Engine
from agentkit.tools.base import ToolSpec
from agentkit.tools.registry import ToolRegistry

from .conftest import ScriptedModel, assistant_text, assistant_tools


def _run_events(events: list) -> TraceBuilder:
    builder = TraceBuilder(provider="deepseek", model="deepseek-flash")
    for event in events:
        builder.consume(event)
    return builder


class TestProviderName:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("deepseek-flash", "deepseek"),
            ("gpt-4o", "unknown"),
            ("qwen-max", "unknown"),
        ],
    )
    def test_infers_from_model(self, model: str, expected: str):
        assert provider_name_for(model=model) == expected

    def test_infers_from_base_url(self):
        assert provider_name_for(base_url="https://api.deepseek.com") == "deepseek"

    def test_unknown_is_not_guessed(self):
        """``gen_ai.provider.name`` 的取值是有约定的枚举，填错比填 unknown 更糟。"""
        assert provider_name_for(base_url="https://example.com", model="mystery") == "unknown"


class TestSpanTree:
    def test_root_span_on_run_started(self):
        builder = _run_events(
            [RunStarted(seq=1, run_id="r1", agent="agent", model="deepseek-flash")]
        )
        span = builder.trace.root
        assert span is not None
        assert span.name == "invoke_agent agent"
        assert span.kind is SpanKind.AGENT
        assert span.attributes["gen_ai.operation.name"] == "invoke_agent"
        assert span.attributes["gen_ai.provider.name"] == "deepseek"

    def test_uses_provider_name_not_deprecated_system(self):
        """``gen_ai.system`` 已被弃用，规范改推 ``gen_ai.provider.name``。"""
        builder = _run_events([RunStarted(seq=1, run_id="r1", agent="a", model="m")])
        assert "gen_ai.provider.name" in builder.trace.root.attributes
        assert "gen_ai.system" not in builder.trace.root.attributes

    def test_conversation_id_recorded_when_given(self):
        builder = TraceBuilder(session_id="s1")
        builder.consume(RunStarted(seq=1, run_id="r1", agent="a", model="m"))
        assert builder.trace.root.attributes["gen_ai.conversation.id"] == "s1"

    def test_chat_span_opens_on_step_and_closes_on_usage(self):
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                StepStarted(seq=2, run_id="r1", step=1),
                UsageReported(
                    seq=3,
                    run_id="r1",
                    step=1,
                    usage=Usage(input_tokens=100, output_tokens=20),
                    cumulative=Usage(input_tokens=100, output_tokens=20),
                ),
            ]
        )
        chat = builder.trace.root.children[0]
        assert chat.name == "chat deepseek-flash"
        assert chat.kind is SpanKind.CHAT
        assert chat.attributes["gen_ai.usage.input_tokens"] == 100
        assert chat.status is SpanStatus.OK
        assert not chat.open

    def test_tool_span_nests_under_root(self):
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                ToolCallStarted(
                    seq=2,
                    run_id="r1",
                    tool_use_id="c1",
                    tool_name="read_file",
                    arguments={"file_path": "a.txt"},
                ),
                ToolResult(
                    seq=3,
                    run_id="r1",
                    tool_use_id="c1",
                    tool_name="read_file",
                    content="内容",
                    duration_ms=3,
                ),
            ]
        )
        tool = builder.trace.root.children[0]
        assert tool.name == "execute_tool read_file"
        assert tool.attributes["gen_ai.tool.name"] == "read_file"
        assert tool.attributes["gen_ai.tool.type"] == "function"
        assert tool.attributes["gen_ai.tool.call.id"] == "c1"
        assert tool.status is SpanStatus.OK

    def test_failed_tool_marks_the_span_not_the_root(self):
        """工具内部失败不等于整个 agent 失败——错误记在工具 span 上。"""
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                ToolCallStarted(seq=2, run_id="r1", tool_use_id="c1", tool_name="f"),
                ToolResult(
                    seq=3,
                    run_id="r1",
                    tool_use_id="c1",
                    tool_name="f",
                    content="ToolValidationError: 参数不合法",
                    is_error=True,
                ),
                RunCompleted(seq=4, run_id="r1", text="完成", steps=1),
            ]
        )
        tool = builder.trace.root.children[0]
        assert tool.status is SpanStatus.ERROR
        assert tool.attributes["error.type"] == "ToolValidationError"
        assert builder.trace.root.status is SpanStatus.OK

    def test_content_is_not_captured_by_default(self):
        """内容捕获是 opt-in——结果可能很大，也可能不该进日志。"""
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                StepStarted(seq=2, run_id="r1", step=1),
                TextDelta(seq=3, run_id="r1", text="答案"),
            ]
        )
        assert "gen_ai.output.messages" not in builder.trace.root.children[0].attributes

    def test_content_captured_when_enabled(self):
        builder = TraceBuilder(capture_content=True)
        for event in [
            RunStarted(seq=1, run_id="r1", agent="a", model="m"),
            StepStarted(seq=2, run_id="r1", step=1),
            TextDelta(seq=3, run_id="r1", text="答"),
            TextDelta(seq=4, run_id="r1", text="案"),
        ]:
            builder.consume(event)
        assert builder.trace.root.children[0].attributes["gen_ai.output.messages"] == "答案"

    def test_run_completed_records_totals(self):
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                RunCompleted(
                    seq=2,
                    run_id="r1",
                    text="好了",
                    steps=2,
                    usage=Usage(input_tokens=500, output_tokens=60),
                    cost=0.0012,
                    currency="CNY",
                    duration_ms=1234,
                ),
            ]
        )
        root = builder.trace.root
        assert root.status is SpanStatus.OK
        assert root.attributes["agentkit.steps"] == 2
        assert root.attributes["agentkit.cost"] == 0.0012
        assert not root.open

    def test_run_failed_marks_error(self):
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                RunFailed(
                    seq=2, run_id="r1", error_type="BudgetExceeded", message="超了", steps=3
                ),
            ]
        )
        root = builder.trace.root
        assert root.status is SpanStatus.ERROR
        assert root.attributes["error.type"] == "BudgetExceeded"

    def test_cancel_is_not_an_error(self):
        """取消是调用方主动做的决定，不是故障——否则监控面板上正常的取消会显示成报错。"""
        from agentkit.core.events import RunCancelled

        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                RunCancelled(seq=2, run_id="r1", reason="用户取消", steps=1),
            ]
        )
        assert builder.trace.root.status is SpanStatus.UNSET
        assert builder.trace.root.events[0]["name"] == "cancelled"

    def test_approval_becomes_a_span_event(self):
        from agentkit.core.events import ApprovalRequested, ApprovalResolved

        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                ToolCallStarted(seq=2, run_id="r1", tool_use_id="c1", tool_name="run_command"),
                ApprovalRequested(
                    seq=3,
                    run_id="r1",
                    tool_use_id="c1",
                    tool_name="run_command",
                    reason="包含写操作",
                ),
                ApprovalResolved(seq=4, run_id="r1", tool_use_id="c1", approved=True),
                ToolResult(seq=5, run_id="r1", tool_use_id="c1", tool_name="run_command"),
            ]
        )
        tool = builder.trace.root.children[0]
        names = [e["name"] for e in tool.events]
        assert names == ["approval_requested", "approval_resolved"]

    def test_unknown_event_types_are_ignored(self):
        """事件类型会增加，追踪器不该因为不认识就崩。"""
        builder = TraceBuilder()
        builder.consume(RunStarted(seq=1, run_id="r1", agent="a", model="m"))

        class Weird:
            type = "some_future_event"

        builder.consume(Weird())  # type: ignore[arg-type]

    def test_finish_closes_dangling_spans(self):
        """异常的事件序列不该在 trace 里留下永不结束的 span——那看起来像程序卡住了。"""
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="a", model="m"),
                StepStarted(seq=2, run_id="r1", step=1),
                ToolCallStarted(seq=3, run_id="r1", tool_use_id="c1", tool_name="f"),
                # 没有对应的 result / completed
            ]
        )
        trace = builder.finish()
        assert not trace.root.open
        assert not trace.root.children[0].open


class TestTraceRendering:
    def test_renders_a_tree(self):
        builder = _run_events(
            [
                RunStarted(seq=1, run_id="r1", agent="agent", model="deepseek-flash"),
                StepStarted(seq=2, run_id="r1", step=1),
                UsageReported(
                    seq=3, run_id="r1", step=1, usage=Usage(input_tokens=10, output_tokens=2)
                ),
                RunCompleted(seq=4, run_id="r1", text="好", steps=1),
            ]
        )
        rendered = builder.trace.render()
        assert "invoke_agent agent" in rendered
        assert "chat deepseek-flash" in rendered
        assert "入 10 / 出 2" in rendered

    def test_empty_trace_renders_placeholder(self):
        assert "空" in TraceBuilder().trace.render()

    def test_to_dict_is_json_serializable(self):
        builder = _run_events([RunStarted(seq=1, run_id="r1", agent="a", model="m")])
        payload = json.dumps(builder.trace.to_dict(), ensure_ascii=False)
        assert "invoke_agent a" in payload


class TestExporters:
    def test_jsonl_round_trip(self, tmp_path: Path):
        builder = _run_events(
            [RunStarted(seq=1, run_id="r1", agent="a", model="m"), RunCompleted(seq=2, run_id="r1")]
        )
        path = tmp_path / "traces.jsonl"
        JsonlExporter(path).export(builder.trace)

        records = read_jsonl(path)
        assert len(records) == 1
        assert records[0]["run_id"] == "r1"
        assert records[0]["root"]["name"] == "invoke_agent a"

    def test_jsonl_appends(self, tmp_path: Path):
        path = tmp_path / "traces.jsonl"
        exporter = JsonlExporter(path)
        for i in range(3):
            builder = _run_events(
                [RunStarted(seq=1, run_id=f"r{i}", agent="a", model="m")]
            )
            exporter.export(builder.trace)
        assert len(read_jsonl(path)) == 3

    def test_read_jsonl_skips_broken_lines(self, tmp_path: Path):
        """一个被截断的尾行不该让整份日志读不出来。"""
        path = tmp_path / "traces.jsonl"
        path.write_text('{"run_id": "ok"}\n{ 截断的\n', encoding="utf-8")
        records = read_jsonl(path)
        assert len(records) == 1 and records[0]["run_id"] == "ok"

    def test_read_jsonl_missing_file(self, tmp_path: Path):
        assert read_jsonl(tmp_path / "nope.jsonl") == []

    def test_memory_exporter(self):
        exporter = MemoryExporter()
        for i in range(2):
            builder = _run_events(
                [RunStarted(seq=1, run_id=f"r{i}", agent="a", model="m")]
            )
            exporter.export(builder.trace)
        assert len(exporter) == 2

    def test_console_exporter(self, capsys):
        builder = _run_events(
            [RunStarted(seq=1, run_id="r1", agent="agent", model="m")]
        )
        ConsoleExporter().export(builder.trace)
        assert "invoke_agent agent" in capsys.readouterr().out


class TestObserverEndToEnd:
    async def test_trace_matches_a_real_engine_run(self, workspace):
        """喂一段真实引擎跑出来的事件流，检查 span 树是否合理。

        这里不用手搓事件——手搓的会跟着实现一起漂移，测不出契约被破坏。
        """
        registry = ToolRegistry()

        async def read_file(ctx, file_path: str) -> str:
            """读文件。"""
            return "文件内容"

        registry.add(ToolSpec.from_function(read_file))
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "a.txt"})),
            assistant_text("读完了"),
        )
        engine = Engine(model, registry, workspace=workspace)

        observer = Observer(provider="deepseek", model="fake-model")
        async for _ in observer.wrap(engine.run("读个文件")):
            pass

        trace = observer.trace
        assert trace is not None
        root = trace.root
        assert root.name == "invoke_agent agent"
        assert root.status is SpanStatus.OK

        kinds = [c.kind for c in root.children]
        assert kinds == [SpanKind.CHAT, SpanKind.TOOL, SpanKind.CHAT]

        tool_span = root.children[1]
        assert tool_span.name == "execute_tool read_file"
        assert tool_span.status is SpanStatus.OK

        # 两轮的 token 应当累加到根 span
        assert root.attributes["gen_ai.usage.input_tokens"] == 200

    async def test_observer_exports_even_if_the_caller_breaks_early(self, workspace):
        """调用方中途 break（比如 HTTP 连接断了）时 trace 仍要收尾并导出。

        **但必须显式关闭。** ``async for ... : break`` 不会同步关掉异步生成器——
        Python 只在它被回收时（通过事件循环的 finalizer 钩子）才调 ``aclose()``，
        时机不确定。所以调用方要用 ``contextlib.aclosing`` 把「收尾」这件事
        变成确定性的。服务端的 ``_pump`` 就是这么写的。
        """
        exporter = MemoryExporter()
        observer = Observer(exporters=[exporter])
        model = ScriptedModel(assistant_text("好"))
        engine = Engine(model, ToolRegistry(), workspace=workspace)

        async with contextlib.aclosing(observer.wrap(engine.run("你好"))) as events:
            async for event in events:
                if event.type == "run_started":
                    break

        assert len(exporter) == 1
        assert observer.trace is not None
        assert observer.trace.root is not None
