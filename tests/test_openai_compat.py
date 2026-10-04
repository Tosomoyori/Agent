"""OpenAI 兼容适配器的双向转换与 usage 归一化。

这些都是纯函数，能在不碰网络的前提下覆盖全部分支——这是把它们从适配器类里
拆出来单独导出的理由。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentkit.core.types import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from agentkit.llm.openai_compat import (
    normalize_usage,
    parse_tool_arguments,
    to_openai_messages,
    to_openai_tools,
    to_stream_chunk,
)


class TestToOpenAIMessages:
    def test_system_and_user(self):
        result = to_openai_messages([Message.system("你是助手"), Message.user("你好")])
        assert result == [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]

    def test_tool_use_becomes_tool_calls_with_string_arguments(self):
        """OpenAI 的 arguments 是**字符串**，不是对象——这是最容易漏的一处转换。"""
        result = to_openai_messages(
            [
                Message.assistant(
                    ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"})
                )
            ]
        )
        assert result[0]["tool_calls"] == [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path": "a.txt"}'},
            }
        ]

    def test_tool_results_become_separate_tool_messages(self):
        """内部的 user 消息里的 content block，在 OpenAI 里是独立的 role:"tool" 消息。"""
        result = to_openai_messages(
            [
                Message.from_tool_results(
                    [
                        ToolResultBlock(tool_use_id="c1", content="甲"),
                        ToolResultBlock(tool_use_id="c2", content="乙"),
                    ]
                )
            ]
        )
        assert result == [
            {"role": "tool", "tool_call_id": "c1", "content": "甲"},
            {"role": "tool", "tool_call_id": "c2", "content": "乙"},
        ]

    def test_reasoning_maps_to_reasoning_content(self):
        """DeepSeek 要求多轮工具调用时原样回传 reasoning_content，否则 400。"""
        result = to_openai_messages(
            [Message.assistant(ReasoningBlock(text="思考过程"), TextBlock(text="答复"))]
        )
        assert result[0]["reasoning_content"] == "思考过程"
        assert result[0]["content"] == "答复"

    def test_assistant_without_text_has_null_content(self):
        result = to_openai_messages(
            [Message.assistant(ToolUseBlock(id="c1", name="f", input={}))]
        )
        assert result[0]["content"] is None
        assert "tool_calls" in result[0]

    def test_full_conversation_shape(self):
        messages = [
            Message.system("sys"),
            Message.user("读文件"),
            Message.assistant(ToolUseBlock(id="c1", name="read_file", input={"p": "a"})),
            Message.from_tool_results([ToolResultBlock(tool_use_id="c1", content="内容")]),
            Message.assistant(TextBlock(text="读完了")),
        ]
        roles = [m["role"] for m in to_openai_messages(messages)]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]


class TestToStreamChunk:
    """原始 SSE chunk → 归一化增量的转换。"""

    def test_text_delta(self):
        chunk = to_stream_chunk(
            {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]}
        )
        assert chunk.text == "你好"
        assert chunk.reasoning == ""
        assert chunk.tool_calls == []

    def test_reasoning_content_is_picked_up(self):
        """reasoning_content 不在标准字段上，SDK 会把它放进 model_extra。"""
        chunk = to_stream_chunk(
            {"choices": [{"delta": {"reasoning_content": "让我想想"}}]}
        )
        assert chunk.reasoning == "让我想想"

    def test_finish_reason_captured(self):
        chunk = to_stream_chunk(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        )
        assert chunk.finish_reason == "tool_calls"

    def test_absent_finish_reason_stays_none(self):
        """大多数 chunk 没有 finish_reason，不能把它记成 None 值以外的任何东西。"""
        chunk = to_stream_chunk({"choices": [{"delta": {"content": "x"}}]})
        assert chunk.finish_reason is None

    def test_tool_call_delta(self):
        chunk = to_stream_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"file_path"',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        assert len(chunk.tool_calls) == 1
        delta = chunk.tool_calls[0]
        assert delta.index == 0
        assert delta.id == "call_1"
        assert delta.name == "read_file"
        assert delta.arguments == '{"file_path"'

    def test_continuation_delta_leaves_id_and_name_none(self):
        """实测行为：id / name 只在首个增量出现，后续 chunk 完全没有这两个字段。

        保持 None 是刻意的——累加器据此知道"这次没有新值"，而不是把 id 覆盖成空串。
        """
        chunk = to_stream_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ': "a.txt"}'}}
                            ]
                        }
                    }
                ]
            }
        )
        assert chunk.tool_calls[0].id is None
        assert chunk.tool_calls[0].name is None
        assert chunk.tool_calls[0].arguments == ': "a.txt"}'

    def test_usage_only_chunk_has_empty_choices(self):
        """实测行为：带 usage 的收尾 chunk，choices 是**空列表**，不是缺字段。

        当成"没有内容"丢掉就会永远拿不到 usage。
        """
        chunk = to_stream_chunk(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 40,
                    "prompt_cache_hit_tokens": 200,
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            }
        )
        assert chunk.usage is not None
        assert chunk.usage.input_tokens == 300
        assert chunk.usage.cached_input_tokens == 200
        assert chunk.usage.reasoning_tokens == 10

    def test_usage_attached_to_a_normal_chunk(self):
        chunk = to_stream_chunk(
            {
                "choices": [{"delta": {"content": "x"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }
        )
        assert chunk.text == "x"
        assert chunk.usage is not None and chunk.usage.input_tokens == 5


class TestParseToolArguments:
    @pytest.mark.parametrize("raw", [None, "", "   ", "null", "none", "{}"])
    def test_empty_forms_are_not_errors(self, raw):
        """模型用 null/空串表示「无参数」，那不是坏参数。"""
        assert parse_tool_arguments(raw) == ({}, None)

    def test_valid_object(self):
        assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, None)

    def test_invalid_json_returns_raw_for_feedback(self):
        """坏 JSON 不抛异常——原样返回，让它作为一次参数错误回灌给模型修正。"""
        parsed, raw = parse_tool_arguments('{"a": ')
        assert parsed == {}
        assert raw == '{"a": '

    def test_valid_json_but_not_object_is_treated_as_bad(self):
        parsed, raw = parse_tool_arguments('["a", "b"]')
        assert parsed == {}
        assert raw == '["a", "b"]'

    def test_unicode_is_not_escaped(self):
        from agentkit.llm.openai_compat import to_openai_messages

        result = to_openai_messages(
            [Message.assistant(ToolUseBlock(id="c1", name="f", input={"q": "中文"}))]
        )
        # ensure_ascii=False：省 token 也让日志可读
        assert "中文" in result[0]["tool_calls"][0]["function"]["arguments"]


class TestNormalizeUsage:
    def test_none(self):
        assert normalize_usage(None).total_tokens == 0

    def test_openai_nested_shape(self):
        raw = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 50},
        }
        usage = normalize_usage(raw)
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 200
        assert usage.cached_input_tokens == 800
        assert usage.reasoning_tokens == 50

    def test_deepseek_top_level_cache_fields(self):
        """DeepSeek 把缓存命中数放在顶层，位置和 OpenAI 不同。"""
        raw = SimpleNamespace(
            prompt_tokens=500, completion_tokens=100, prompt_cache_hit_tokens=400
        )
        usage = normalize_usage(raw)
        assert usage.cached_input_tokens == 400

    def test_anthropic_shape(self):
        raw = {
            "input_tokens": 300,
            "output_tokens": 60,
            "cache_read_input_tokens": 200,
        }
        usage = normalize_usage(raw)
        assert usage.input_tokens == 300
        assert usage.output_tokens == 60
        assert usage.cached_input_tokens == 200

    def test_subset_arithmetic_avoids_double_billing(self):
        """缓存命中是 prompt 的**子集**，不相减就会重复计费。"""
        raw = {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 900},
        }
        usage = normalize_usage(raw)
        assert usage.billable_input_tokens == 100
        assert usage.total_tokens == 1100  # 上下文占用量，不是计费量

    def test_accumulation(self):
        a = normalize_usage({"prompt_tokens": 10, "completion_tokens": 2})
        b = normalize_usage({"prompt_tokens": 20, "completion_tokens": 3})
        assert (a + b).input_tokens == 30
        assert (a + b).output_tokens == 5


class TestToOpenAITools:
    def test_shape(self):
        schemas = [
            ToolSchema(name="f", description="d", parameters={"type": "object"})
        ]
        assert to_openai_tools(schemas) == [
            {
                "type": "function",
                "function": {
                    "name": "f",
                    "description": "d",
                    "parameters": {"type": "object"},
                },
            }
        ]
