"""共享测试夹具。

测试策略的核心手法在这里：**把不确定性全部收敛到 ``ChatModel`` 边界**。

Agent 的行为之所以难以测试，是因为模型的输出不确定。但只要在模型边界放一个
「按脚本返回预设响应」的假实现，上层（引擎循环、工具执行、事件流、消息转换）
就完全确定了——不碰网络、不花钱、结果可复现。

真正需要真实模型的部分（provider 协议行为、评测抽样）另走 ``-m live``，
见 ``tests/test_live.py``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from agentkit.core.types import Message, TextBlock, ToolUseBlock
from agentkit.core.usage import Usage
from agentkit.llm.base import ChatModel, ModelCapabilities, StreamChunk, ToolCallDelta
from agentkit.tools.base import ToolContext
from agentkit.tools.registry import ToolRegistry

#: 脚本里可以出现的东西：一条完整消息、一组预先切好的流式增量、或者一个异常。
ScriptItem = Message | Sequence[StreamChunk] | Exception


class ScriptedModel(ChatModel):
    """按预设脚本依次返回响应的假模型。

    脚本项有三种：

    * :class:`Message` —— 自动切成一个 chunk（正文/思维链/工具调用各一个增量）；
    * ``Sequence[StreamChunk]`` —— 手工指定增量序列，用来测逐 token 的流式行为；
    * 异常实例 —— 抛出来，用来测错误路径与重试。

    **只实现 ``stream()``，不实现 ``complete()``。** 基类的 ``complete()`` 由
    ``accumulate(stream())`` 而来，所以引擎的非流式路径也会顺带把累加逻辑测了——
    如果这里单独实现 ``complete()``，就出现了两条组装路径，而其中一条永远不被测试。
    """

    def __init__(
        self,
        *responses: ScriptItem,
        name: str = "fake-model",
        usage: Usage | None = None,
    ) -> None:
        self.script = list(responses)
        self.name = name
        self.capabilities = ModelCapabilities()
        self._usage = usage or Usage(
            input_tokens=100, output_tokens=20, cached_input_tokens=40
        )

        #: 每次调用时收到的完整消息列表，用来断言引擎发出去的历史是否正确。
        self.calls: list[list[Message]] = []
        #: 每次调用时收到的工具 schema。
        self.tool_schemas: list[list | None] = []
        #: 每次调用时收到的额外生成参数。
        self.generation_kwargs: list[dict] = []

    def stream(
        self,
        *,
        messages: list[Message],
        tools=None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        # 调用信息在这里就记下来（同步），不等迭代开始——否则「引擎调用了几次」
        # 这类断言会因为惰性求值而对不上。
        self.calls.append(list(messages))
        self.tool_schemas.append(tools)
        self.generation_kwargs.append(kwargs)
        return self._generate()

    async def _generate(self) -> AsyncIterator[StreamChunk]:
        if not self.script:
            raise AssertionError(
                f"脚本已耗尽，但引擎发起了第 {len(self.calls)} 次调用——"
                "说明循环次数比预期多。"
            )

        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        if isinstance(item, Message):
            yield _usage_chunk(self._to_chunk(item), self._usage)
            return

        # 预先切好的增量序列：最后一个 chunk 补上 usage
        chunks = list(item)
        for index, chunk in enumerate(chunks):
            if index == len(chunks) - 1:
                yield _usage_chunk(chunk, self._usage)
            else:
                yield chunk

    @staticmethod
    def _to_chunk(message: Message) -> StreamChunk:
        """把一条完整消息切成一个流式增量。"""
        chunk = StreamChunk(
            text=message.text(),
            reasoning=message.reasoning_text(),
            finish_reason="tool_calls" if message.tool_uses() else "stop",
        )
        for index, use in enumerate(message.tool_uses()):
            chunk.tool_calls.append(
                ToolCallDelta(
                    index=index,
                    id=use.id,
                    name=use.name,
                    arguments=json.dumps(use.input, ensure_ascii=False),
                )
            )
        return chunk

    @property
    def remaining(self) -> int:
        return len(self.script)


def _usage_chunk(chunk: StreamChunk, usage: Usage) -> StreamChunk:
    """给最后一个增量带上 usage——真实 provider 就是这么做的。

    实测确认：usage 只在收尾的那个 chunk 里出现。
    """
    chunk.usage = usage
    return chunk


def text_chunks(text: str, *, size: int = 1) -> list[StreamChunk]:
    """把一段文本切成一字一个增量，用来测逐 token 的流式路径。"""
    return [StreamChunk(text=text[i : i + size]) for i in range(0, len(text), size)]


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
