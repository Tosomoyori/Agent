"""流式增量的累加。

这些用例固定的是**实测出来的 provider 行为**（见 ``scripts/capture_stream.py``），
不是照文档猜的：

* ``arguments`` 是增量（``''`` → ``'{'`` → ``'"'`` → …），不是累积重发；
* ``id`` / ``name`` 只在首个增量出现，后续 chunk 完全没有这两个字段；
* usage 只在收尾 chunk 出现，且那个 chunk 的 ``choices`` 是**空列表**。
"""

from __future__ import annotations

import json

import pytest

from agentkit.llm._json import is_complete_json_object, join_argument_fragments
from agentkit.llm.base import StreamAccumulator, StreamChunk, ToolCallDelta, accumulate


def _call(index: int = 0, **kwargs) -> StreamChunk:
    return StreamChunk(tool_calls=[ToolCallDelta(index=index, **kwargs)])


class TestJoinArgumentFragments:
    def test_incremental_fragments_are_concatenated(self):
        fragments = ["", "{", '"file_path"', ": ", '"/etc/hosts"', "}"]
        joined, note = join_argument_fragments(fragments)
        assert json.loads(joined) == {"file_path": "/etc/hosts"}
        assert note is None

    def test_cumulative_resend_is_detected_and_recovered(self):
        """部分 provider（如百度千帆）每片重发全部内容，直接拼接会拼出两个对象。

        这种情况要能识别并取最长的那一片，而且**必须留痕**——静默地做对，
        比做错了还让人放心不下。
        """
        fragments = ['{"a"', '{"a": 1', '{"a": 1}']
        joined, note = join_argument_fragments(fragments)
        assert json.loads(joined) == {"a": 1}
        assert note is not None and "累积重发" in note

    def test_unparseable_fragments_are_returned_as_is(self):
        """拼不出合法对象时不猜——原样交出去，让上层按坏 JSON 回灌给模型。"""
        joined, note = join_argument_fragments(['{"a":', " oops"])
        assert joined == '{"a": oops'
        assert note is None

    def test_empty_input(self):
        assert join_argument_fragments([]) == ("", None)

    def test_single_digit_fragment_is_not_a_complete_object(self):
        """实测里 arguments 分片出现过单独的 ``"8"``。

        ``json.loads("8")`` 是合法的，但那只是个数字。用它判断"参数拼完整了吗"
        会得出错误结论——所以判据必须是"合法 JSON **对象**"。
        """
        assert json.loads("8") == 8  # 合法 JSON
        assert not is_complete_json_object("8")  # 但不是对象


class TestStreamAccumulator:
    def test_text_and_reasoning_are_concatenated(self):
        acc = StreamAccumulator()
        for chunk in (StreamChunk(text="你"), StreamChunk(reasoning="想"), StreamChunk(text="好")):
            acc.feed(chunk)
        assert acc.text == "你好"
        assert acc.reasoning == "想"

    def test_id_and_name_are_assigned_not_appended(self):
        """id/name 只在首个增量出现；用 ``+=`` 拼会把 id 变成一串垃圾。"""
        acc = StreamAccumulator()
        acc.feed(_call(0, id="call_1", name="read_file", arguments="{"))
        acc.feed(_call(0, arguments='"a":1}'))

        use = acc.message().tool_uses()[0]
        assert use.id == "call_1"
        assert use.name == "read_file"
        assert use.input == {"a": 1}

    def test_arguments_accumulate_across_chunks(self):
        acc = StreamAccumulator()
        for piece in ['{"file_path"', ': "', '/etc/hosts", ', '"encoding": "utf-8"', "}"]:
            acc.feed(_call(0, arguments=piece))

        use = acc.message().tool_uses()[0]
        assert use.input == {"file_path": "/etc/hosts", "encoding": "utf-8"}

    def test_parallel_calls_are_grouped_by_index(self):
        """并行调用靠 ``index`` 区分——按出现顺序分组会把两个调用的参数混在一起。"""
        acc = StreamAccumulator()
        acc.feed(
            StreamChunk(
                tool_calls=[
                    ToolCallDelta(index=0, id="c0", name="read_file"),
                    ToolCallDelta(index=1, id="c1", name="list_directory"),
                ]
            )
        )
        # 两个调用的参数片段交错到达
        acc.feed(
            StreamChunk(
                tool_calls=[
                    ToolCallDelta(index=0, arguments='{"file_path"'),
                    ToolCallDelta(index=1, arguments='{"path"'),
                ]
            )
        )
        acc.feed(
            StreamChunk(
                tool_calls=[
                    ToolCallDelta(index=0, arguments=': "a.txt"}'),
                    ToolCallDelta(index=1, arguments=': "."}'),
                ]
            )
        )

        uses = acc.message().tool_uses()
        assert [u.name for u in uses] == ["read_file", "list_directory"]
        assert uses[0].input == {"file_path": "a.txt"}
        assert uses[1].input == {"path": "."}

    def test_missing_id_gets_a_generated_one(self):
        acc = StreamAccumulator()
        acc.feed(_call(3, name="f", arguments="{}"))
        assert acc.message().tool_uses()[0].id

    def test_bad_json_yields_raw_arguments_for_feedback(self):
        """拼不成合法 JSON 时保留原文，好让引擎把它回灌给模型。"""
        acc = StreamAccumulator()
        acc.feed(_call(0, name="f", arguments='{"a": '))
        use = acc.message().tool_uses()[0]
        assert use.input == {}
        assert use.raw_arguments == '{"a": '

    def test_usage_is_captured_from_the_final_chunk(self):
        from agentkit.core.usage import Usage

        acc = StreamAccumulator()
        acc.feed(StreamChunk(text="x"))
        assert acc.usage.total_tokens == 0
        acc.feed(StreamChunk(usage=Usage(input_tokens=10, output_tokens=2)))
        assert acc.usage.input_tokens == 10

    def test_finish_reason_captured(self):
        acc = StreamAccumulator()
        acc.feed(StreamChunk(finish_reason="tool_calls"))
        assert acc.finish_reason == "tool_calls"

    def test_empty_stream_produces_empty_message(self):
        acc = StreamAccumulator()
        message = acc.message()
        assert message.is_empty()
        assert message.role == "assistant"


class TestAccumulate:
    async def test_collects_a_stream_into_a_completion(self):
        async def stream():
            yield StreamChunk(reasoning="想")
            yield StreamChunk(text="答")
            yield StreamChunk(
                tool_calls=[ToolCallDelta(index=0, id="c0", name="f", arguments="{}")],
                finish_reason="tool_calls",
            )

        completion = await accumulate(stream())
        assert completion.message.reasoning_text() == "想"
        assert completion.message.text() == "答"
        assert completion.message.tool_uses()[0].name == "f"
        assert completion.finish_reason == "tool_calls"

    async def test_empty_stream_is_not_an_error(self):
        async def stream():
            return
            yield  # pragma: no cover

        completion = await accumulate(stream())
        assert completion.finish_reason == "stop"


@pytest.mark.parametrize("size", [1, 2, 5])
async def test_chunking_does_not_change_the_result(size: int):
    """分片粒度不该影响结果——这是累加逻辑正确与否的基本检验。"""

    async def stream():
        for i in range(0, 10, size):
            yield StreamChunk(text="0123456789"[i : i + size])

    completion = await accumulate(stream())
    assert completion.message.text() == "0123456789"
