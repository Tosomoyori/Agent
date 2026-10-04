"""引擎循环：事件序列、消息配对、错误回灌、步数上限。

因为有 :class:`ScriptedModel`，这些测试全部离线、确定性、可复现——
不需要 mock 时间，也不需要给断言留容差。
"""

from __future__ import annotations

from typing import Annotated

import pytest

from agentkit.core.errors import RateLimitError
from agentkit.core.types import Message, ToolUseBlock, validate_conversation
from agentkit.runtime.engine import Engine, collect
from agentkit.tools.base import ToolSpec
from agentkit.tools.registry import ToolRegistry

from .conftest import ScriptedModel, assistant_text, assistant_tools


def _make_engine(model, workspace, registry, **kwargs) -> Engine:
    return Engine(model, registry, workspace=workspace, **kwargs)


def _event_types(events) -> list[str]:
    return [e.type for e in events]


async def _drain(engine, prompt: str):
    return [event async for event in engine.run(prompt)]


@pytest.fixture
def read_tool() -> ToolSpec:
    async def read_file(ctx, file_path: Annotated[str, "路径"]) -> str:
        """读取文件。"""
        path = ctx.resolve(file_path)
        return path.read_text(encoding="utf-8")

    return ToolSpec.from_function(read_file)


# ---------------------------------------------------------------- 正常路径


class TestHappyPath:
    async def test_direct_answer_without_tools(self, workspace):
        model = ScriptedModel(assistant_text("直接回答"))
        engine = _make_engine(model, workspace, ToolRegistry())

        events = await _drain(engine, "你好")
        assert _event_types(events) == [
            "run_started",
            "step_started",
            "usage_reported",
            "text_delta",
            "run_completed",
        ]
        assert events[-1].text == "直接回答"
        assert events[-1].steps == 1

    async def test_tool_call_then_answer(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("文件有三行"),
        )
        engine = _make_engine(model, workspace, registry)

        events = await _drain(engine, "读 hello.txt")
        types = _event_types(events)

        assert types == [
            "run_started",
            "step_started",
            "usage_reported",
            "tool_call_started",
            "tool_result",
            "step_started",
            "usage_reported",
            "text_delta",
            "run_completed",
        ]
        assert events[-1].text == "文件有三行"
        assert events[-1].steps == 2

    async def test_tool_result_reaches_the_model(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("好了"),
        )
        await _drain(_make_engine(model, workspace, registry), "读文件")

        # 第二次调用时，模型应当能看到第一行、第三行这样的真实内容
        second_call = model.calls[1]
        assert "第一行" in second_call[-1].text() or any(
            "第一行" in r.content for r in second_call[-1].tool_results()
        )

    async def test_message_history_keeps_pairing_invariant(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("好了"),
        )
        await _drain(_make_engine(model, workspace, registry), "读文件")

        # 引擎发出的每一份历史都必须满足配对不变量
        for call in model.calls:
            validate_conversation(call)

    async def test_parallel_tool_calls_share_one_user_message(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(
                ("read_file", {"file_path": "hello.txt"}),
                ("read_file", {"file_path": "sub/nested.txt"}),
            ),
            assistant_text("都读了"),
        )
        events = await _drain(_make_engine(model, workspace, registry), "读两个文件")

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 2

        # 两条结果必须在**同一条** user 消息里
        final_history = model.calls[1]
        assert len(final_history[-1].tool_results()) == 2
        assert final_history[-1].role == "user"

    async def test_system_prompt_is_prepended(self, workspace):
        model = ScriptedModel(assistant_text("好"))
        await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")
        assert model.calls[0][0].role == "system"

    async def test_tool_schemas_are_passed_to_model(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(assistant_text("好"))
        await _drain(_make_engine(model, workspace, registry), "你好")

        schemas = model.tool_schemas[0]
        assert schemas is not None
        assert schemas[0].name == "read_file"

    async def test_no_tools_means_no_tool_param(self, workspace):
        model = ScriptedModel(assistant_text("好"))
        await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")
        assert model.tool_schemas[0] is None

    async def test_generation_kwargs_are_forwarded(self, workspace):
        class RecordingModel(ScriptedModel):
            async def complete(self, *, messages, tools=None, tool_choice="auto", **kwargs):
                self.kwargs = kwargs
                return await super().complete(
                    messages=messages, tools=tools, tool_choice=tool_choice
                )

        model = RecordingModel(assistant_text("好"))
        engine = _make_engine(model, workspace, ToolRegistry(), temperature=0.7, max_tokens=512)
        await _drain(engine, "你好")
        assert model.kwargs == {"temperature": 0.7, "max_tokens": 512}


# ---------------------------------------------------------------- 错误回灌


class TestErrorFeedback:
    async def test_validation_error_is_reflected_and_model_can_recover(
        self, workspace, read_tool
    ):
        """参数写错时，模型应当收到可读的错误并能改对。"""
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"wrong_param": "hello.txt"})),
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("补上了"),
        )
        events = await _drain(_make_engine(model, workspace, registry), "读文件")

        first_result = next(e for e in events if e.type == "tool_result")
        assert first_result.is_error
        assert "file_path" in first_result.content  # 错误信息指名了字段

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 2
        assert results[1].is_error is False
        assert events[-1].text == "补上了"

    async def test_malformed_json_arguments_are_reflected(self, workspace):
        """模型给出不是 JSON 的参数，原文要回灌让它重来。"""
        registry = ToolRegistry()

        async def noop(ctx, x: str = "") -> str:
            """无操作。"""
            return "ok"

        registry.register(noop)

        broken = Message.assistant(
            ToolUseBlock(
                id="c1", name="noop", input={}, raw_arguments='{"x": "unclosed'
            )
        )
        model = ScriptedModel(broken, assistant_text("改了"))
        events = await _drain(_make_engine(model, workspace, registry), "试试")

        result = next(e for e in events if e.type == "tool_result")
        assert result.is_error
        assert "合法的 JSON" in result.content
        assert '{"x": "unclosed' in result.content
        assert events[-1].text == "改了"

    async def test_unknown_tool_is_reflected(self, workspace):
        model = ScriptedModel(
            assistant_tools(("no_such_tool", {})),
            assistant_text("换个办法"),
        )
        events = await _drain(_make_engine(model, workspace, ToolRegistry()), "试试")

        result = next(e for e in events if e.type == "tool_result")
        assert result.is_error
        assert "不存在" in result.content


# ---------------------------------------------------------------- 终止条件


class TestTermination:
    async def test_max_steps_produces_run_failed(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        # 无限调用工具，永不给出最终答复
        never_ends = [
            assistant_tools(("read_file", {"file_path": "hello.txt"})) for _ in range(3)
        ]
        model = ScriptedModel(*never_ends)
        engine = _make_engine(model, workspace, registry, max_steps=3)

        events = await _drain(engine, "读文件")
        assert events[-1].type == "run_failed"
        assert events[-1].error_type == "MaxStepsExceeded"

    async def test_llm_error_produces_run_failed(self, workspace):
        model = ScriptedModel(RateLimitError("请求过多"))
        events = await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")

        assert events[-1].type == "run_failed"
        assert events[-1].error_type == "RateLimitError"
        assert events[-1].steps == 0

    async def test_exactly_one_terminal_event(self, workspace, read_tool):
        """失败或取消时以终止事件收尾，且必居其一、只有一次。"""
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("完成"),
        )
        events = await _drain(_make_engine(model, workspace, registry), "读文件")

        terminal = [
            e for e in events if e.type in {"run_completed", "run_failed", "run_cancelled"}
        ]
        assert len(terminal) == 1


# ---------------------------------------------------------------- 事件流性质


class TestEventStream:
    async def test_seq_is_monotonic_from_one(self, workspace):
        model = ScriptedModel(assistant_text("好"))
        events = await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

    async def test_all_events_share_run_id(self, workspace):
        model = ScriptedModel(assistant_text("好"))
        events = await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")
        assert len({e.run_id for e in events}) == 1
        assert events[0].run_id.startswith("run_")

    async def test_usage_accumulates(self, workspace, read_tool):
        registry = ToolRegistry([read_tool])
        model = ScriptedModel(
            assistant_tools(("read_file", {"file_path": "hello.txt"})),
            assistant_text("好了"),
        )
        events = await _drain(_make_engine(model, workspace, registry), "读文件")

        reported = [e for e in events if e.type == "usage_reported"]
        assert len(reported) == 2
        # 每次调用 100 入 / 20 出，两轮就是 200 / 40
        assert reported[-1].cumulative.input_tokens == 200
        assert reported[-1].cumulative.output_tokens == 40
        assert events[-1].usage.input_tokens == 200

    async def test_events_serialize_to_json(self, workspace):
        """SSE 端点会把事件序列化成 JSON，形状必须稳定。"""
        import json

        model = ScriptedModel(assistant_text("好"))
        events = await _drain(_make_engine(model, workspace, ToolRegistry()), "你好")
        for event in events:
            payload = json.loads(event.model_dump_json())
            assert payload["type"] == event.type


class TestCollect:
    async def test_collect_success(self, workspace):
        model = ScriptedModel(assistant_text("答案"))
        result = await collect(
            _make_engine(model, workspace, ToolRegistry()).run("问题")
        )
        assert result.ok
        assert result.text == "答案"
        assert result.steps == 1
        assert result.usage.input_tokens == 100

    async def test_collect_failure(self, workspace):
        model = ScriptedModel(RateLimitError("限流"))
        result = await collect(
            _make_engine(model, workspace, ToolRegistry()).run("问题")
        )
        assert not result.ok
        assert result.error_type == "RateLimitError"
        assert result.error is not None
