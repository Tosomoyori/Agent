"""内容块消息模型与配对不变量。"""

from __future__ import annotations

import pytest

from agentkit.core.errors import InvalidConversation
from agentkit.core.types import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
    validate_conversation,
)


class TestMessageConstruction:
    def test_text_accessors(self):
        msg = Message.assistant(
            ReasoningBlock(text="让我想想"), TextBlock(text="答案是")
        )
        assert msg.role == "assistant"
        assert msg.text() == "答案是"
        assert msg.reasoning_text() == "让我想想"
        assert msg.tool_uses() == []

    def test_tool_results_are_user_messages(self):
        """工具结果在内部模型里属于 user 消息，与 Anthropic 一致。"""
        block = ToolResultBlock(tool_use_id="c1", content="结果")
        msg = Message.from_tool_results([block])
        assert msg.role == "user"
        assert msg.tool_results() == [block]

    def test_empty_message(self):
        assert Message(role="user").is_empty()


class TestDiscriminatedUnion:
    def test_roundtrip_through_json(self):
        """事件与消息要能序列化——checkpoint 和 SSE 都依赖这一点。"""
        original = Message.assistant(
            TextBlock(text="文本"),
            ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"}),
        )
        restored = Message.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.tool_uses()[0].name == "read_file"


class TestValidateConversation:
    def test_valid_pairing_passes(self):
        messages = [
            Message.user("读个文件"),
            Message.assistant(
                ToolUseBlock(id="c1", name="read_file", input={"file_path": "a"})
            ),
            Message.from_tool_results(
                [ToolResultBlock(tool_use_id="c1", content="内容")]
            ),
            Message.assistant(TextBlock(text="读到了")),
        ]
        validate_conversation(messages)  # 不抛异常即通过

    def test_parallel_calls_in_one_user_message_passes(self):
        """并行工具调用的全部结果必须合并在同一条 user 消息里。"""
        messages = [
            Message.user("同时做两件事"),
            Message.assistant(
                ToolUseBlock(id="c1", name="read_file", input={}),
                ToolUseBlock(id="c2", name="list_directory", input={}),
            ),
            Message.from_tool_results(
                [
                    ToolResultBlock(tool_use_id="c1", content="甲"),
                    ToolResultBlock(tool_use_id="c2", content="乙"),
                ]
            ),
            Message.assistant(TextBlock(text="都好了")),
        ]
        validate_conversation(messages)

    def test_missing_result_raises(self):
        messages = [
            Message.user("读文件"),
            Message.assistant(
                ToolUseBlock(id="c1", name="read_file", input={}),
                ToolUseBlock(id="c2", name="list_directory", input={}),
            ),
            # 只回了一个结果
            Message.from_tool_results([ToolResultBlock(tool_use_id="c1", content="甲")]),
        ]
        with pytest.raises(InvalidConversation, match="缺少 1 个 tool_result"):
            validate_conversation(messages)

    def test_orphan_result_raises(self):
        messages = [
            Message.user("你好"),
            Message.from_tool_results([ToolResultBlock(tool_use_id="ghost", content="?")]),
        ]
        with pytest.raises(InvalidConversation, match="找不到对应的 tool_use"):
            validate_conversation(messages)

    def test_duplicate_tool_use_id_raises(self):
        messages = [
            Message.user("x"),
            Message.assistant(
                ToolUseBlock(id="dup", name="a", input={}),
                ToolUseBlock(id="dup", name="b", input={}),
            ),
        ]
        with pytest.raises(InvalidConversation, match="重复"):
            validate_conversation(messages)

    def test_dangling_at_end_raises(self):
        messages = [
            Message.user("x"),
            Message.assistant(ToolUseBlock(id="c1", name="a", input={})),
        ]
        with pytest.raises(InvalidConversation, match="对话结束时"):
            validate_conversation(messages)

    def test_error_identifies_offending_index(self):
        """报错要指到具体下标——否则线上排查只能靠猜。"""
        messages = [
            Message.user("x"),
            Message.assistant(ToolUseBlock(id="c1", name="a", input={})),
            Message.from_tool_results([ToolResultBlock(tool_use_id="c1", content="ok")]),
            Message.assistant(ToolUseBlock(id="c2", name="a", input={})),
            Message.from_tool_results([ToolResultBlock(tool_use_id="wrong", content="?")]),
        ]
        with pytest.raises(InvalidConversation, match="第 4 条 user 消息"):
            validate_conversation(messages)


class TestToolSchema:
    def test_openai_tool_shape(self):
        schema = ToolSchema(
            name="read_file",
            description="读文件",
            parameters={"type": "object", "properties": {}},
        )
        assert schema.openai_tool() == {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读文件",
                "parameters": {"type": "object", "properties": {}},
            },
        }
