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
    from_openai_message,
    normalize_usage,
    parse_tool_arguments,
    to_openai_messages,
    to_openai_tools,
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


class TestFromOpenAIMessage:
    def test_text_only(self):
        message = from_openai_message(SimpleNamespace(content="hello", tool_calls=None))
        assert message.role == "assistant"
        assert message.text() == "hello"
        assert message.tool_uses() == []

    def test_reasoning_content_is_picked_up(self):
        """reasoning_content 在 pydantic 的 model_extra 里，不在标准字段上。"""
        raw = SimpleNamespace(
            content="答复", tool_calls=None, model_extra={"reasoning_content": "想想"}
        )
        message = from_openai_message(raw)
        assert message.reasoning_text() == "想想"
        assert message.text() == "答复"

    def test_tool_calls_parsed_into_blocks(self):
        raw = SimpleNamespace(
            content=None,
            reasoning_content=None,
            tool_calls=[
                SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(
                        name="read_file", arguments='{"file_path": "a.txt"}'
                    ),
                )
            ],
        )
        message = from_openai_message(raw)
        uses = message.tool_uses()
        assert len(uses) == 1
        assert uses[0].name == "read_file"
        assert uses[0].input == {"file_path": "a.txt"}
        assert uses[0].raw_arguments is None

    def test_missing_tool_call_id_gets_generated(self):
        raw = SimpleNamespace(
            content=None,
            reasoning_content=None,
            tool_calls=[
                SimpleNamespace(
                    id=None, function=SimpleNamespace(name="f", arguments="{}")
                )
            ],
        )
        uses = from_openai_message(raw).tool_uses()
        assert uses[0].id.startswith("call_")


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
