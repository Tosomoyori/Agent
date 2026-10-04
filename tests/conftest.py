"""共享测试夹具。

测试策略的核心手法在这里：**把不确定性全部收敛到 ``ChatModel`` 边界**。

Agent 的行为之所以难以测试，是因为模型的输出不确定。但只要在模型边界放一个
「按脚本返回预设响应」的假实现，上层（引擎循环、工具执行、事件流、消息转换）
就完全确定了——不碰网络、不花钱、结果可复现。

真正需要真实模型的部分（provider 协议行为、评测抽样）另走 ``-m live``，
见 ``tests/test_live.py``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.core.types import Message, TextBlock, ToolUseBlock
from agentkit.core.usage import Usage
from agentkit.llm.base import ChatModel, Completion, ModelCapabilities
from agentkit.tools.base import ToolContext
from agentkit.tools.registry import ToolRegistry


class ScriptedModel(ChatModel):
    """按预设脚本依次返回响应的假模型。

    脚本项可以是 :class:`Message`，也可以是一个异常实例——后者会被抛出来，
    用来测试错误路径。
    """

    def __init__(self, *responses: Message | Exception, name: str = "fake-model") -> None:
        self.script = list(responses)
        self.name = name
        self.capabilities = ModelCapabilities()

        #: 每次调用时收到的完整消息列表，用来断言引擎发出去的历史是否正确。
        self.calls: list[list[Message]] = []
        #: 每次调用时收到的工具 schema。
        self.tool_schemas: list[list | None] = []

    async def complete(
        self,
        *,
        messages: list[Message],
        tools=None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> Completion:
        self.calls.append(list(messages))
        self.tool_schemas.append(tools)

        if not self.script:
            raise AssertionError(
                f"脚本已耗尽，但引擎发起了第 {len(self.calls)} 次调用——"
                "说明循环次数比预期多。"
            )

        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        return Completion(
            message=item,
            finish_reason="stop",
            usage=Usage(input_tokens=100, output_tokens=20, cached_input_tokens=40),
        )

    @property
    def remaining(self) -> int:
        return len(self.script)


# ---------------------------------------------------------------- 构造助手


def assistant_text(text: str) -> Message:
    """一个直接给出最终答复的 assistant 消息。"""
    return Message.assistant(TextBlock(text=text))


def assistant_tools(*calls: tuple[str, dict], id_prefix: str = "call") -> Message:
    """一个请求调用若干工具的 assistant 消息。

    :param calls: ``(工具名, 参数字典)`` 的可变序列。
    """
    return Message.assistant(
        *[
            ToolUseBlock(id=f"{id_prefix}_{index}", name=name, input=arguments)
            for index, (name, arguments) in enumerate(calls)
        ]
    )


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个隔离的临时工作区。"""
    (tmp_path / "hello.txt").write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("嵌套文件\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, run_id="run_test")


@pytest.fixture
def registry() -> ToolRegistry:
    """一个空注册表。测试只注册自己关心的工具，避免相互干扰。"""
    return ToolRegistry()
