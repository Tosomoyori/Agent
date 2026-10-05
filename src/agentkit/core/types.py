"""内容块消息模型——全项目的地基。

不采用 OpenAI 的 ``tool_calls[]`` + ``role: "tool"`` 形状。

该形状只是 OpenAI 一家约定。Anthropic 的 ``tool_use`` / ``tool_result`` 是
assistant / user 消息内部的 content block，**没有独立的 ``tool`` role**，而且同一轮里的
每个 ``tool_use`` 都必须在紧随其后的那条 user 消息里找到配对的 ``tool_result``。

两种做法：
  * 内部抄 OpenAI 的形状 → 每次转 Anthropic 都要重新分组、重排消息，且有损。
  * 内部规范成 content block → 转 Anthropic 是直接映射，转 OpenAI 才需要一次扁平化。

选后者。代价是要自己写双向转换，并在转换层强制断言配对不变量——这个复杂度是为
可移植性付的税，见 :func:`validate_conversation`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from .errors import InvalidConversation

Role = Literal["system", "user", "assistant"]


class TextBlock(BaseModel):
    """普通文本。"""

    type: Literal["text"] = "text"
    text: str


class ReasoningBlock(BaseModel):
    """模型暴露出来的思维链。

    各家叫法不同：DeepSeek 在响应里给 ``reasoning_content`` 字段，Anthropic 用
    ``thinking`` content block。**DeepSeek 要求在多轮工具调用时把它原样回传**，
    否则下一次请求返回 400，所以它必须作为一等公民存在于消息模型里，
    而不是被塞进某个 provider 私有字段。
    """

    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolUseBlock(BaseModel):
    """模型请求调用一个工具。"""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)

    #: 模型给的原始参数字符串。当 ``input`` 为空且此项非空时，说明模型的参数
    #: 不是合法 JSON——保留原文是为了能把它回灌给模型让它自我修正。
    raw_arguments: str | None = None


class ToolResultBlock(BaseModel):
    """工具的执行结果，回灌给模型。"""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Annotated[
    TextBlock | ReasoningBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]


class ToolSchema(BaseModel):
    """工具对模型暴露的契约。

    provider 中立的表示：``parameters`` 是标准 JSON Schema。转成各家格式
    （OpenAI 的 ``tools[].function``、Anthropic 的 ``tools[].input_schema``、
    Gemini 的 ``functionDeclarations``）是各适配器的职责。

    这样 ``agentkit.tools`` 完全不需要知道 OpenAI 的存在。
    """

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def openai_tool(self) -> dict[str, Any]:
        """转成 OpenAI Chat Completions 的 tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Message(BaseModel):
    """一条对话消息。

    工具结果在内部模型里属于 **user** 消息（与 Anthropic 一致），而不是独立的
    ``tool`` role。所有并行工具调用的结果合并进同一条 user 消息。
    """

    role: Role
    content: list[ContentBlock] = Field(default_factory=list)

    # -------------------------------------------------------- 构造

    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role="system", content=[TextBlock(text=text)])

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, *blocks: ContentBlock) -> Message:
        return cls(role="assistant", content=list(blocks))

    @classmethod
    def from_tool_results(cls, results: Sequence[ToolResultBlock]) -> Message:
        """把一组工具结果打包成一条 user 消息。

        即使只有一个结果也走这里，保证「一条 assistant 的 tool_use 对应一条 user 的
        tool_result」这个形状恒定，转换层不需要处理两种情况。

        名字里带 ``from_`` 前缀是为了和下面那个同名的查询方法 :meth:`tool_results`
        区分开——两者同名会让后者静默覆盖前者。
        """
        return cls(role="user", content=list(results))

    # -------------------------------------------------------- 查询

    def text(self) -> str:
        """拼接所有文本块。"""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def reasoning_text(self) -> str:
        """拼接所有思维链块。"""
        return "".join(b.text for b in self.content if isinstance(b, ReasoningBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def tool_results(self) -> list[ToolResultBlock]:
        return [b for b in self.content if isinstance(b, ToolResultBlock)]

    def is_empty(self) -> bool:
        return not self.content


def validate_conversation(messages: Sequence[Message]) -> None:
    """强制消息序列的配对不变量，违反时抛 :class:`InvalidConversation`。

    规则（来自 Anthropic 的硬约束，本框架对内部模型统一施加，因此更换 provider 不会触发该问题）：

    1. 每个 ``tool_use`` 都必须在**紧随其后**的那条 user 消息里找到 ``tool_result``；
    2. 不允许出现没有对应 ``tool_use`` 的孤儿 ``tool_result``；
    3. 并行调用的全部结果必须合并在**同一条** user 消息里，不能拆成多条。

    这个不变量在裁剪历史时同样重要：``tool_use`` 和它的 ``tool_result`` 必须当作
    原子对一起保留或一起删除，否则会留下孤块。

    违反时给出**指到具体下标和 id** 的报错，否则线上排查只能靠猜。
    """
    # tool_use_id -> 期望它出现在哪条 user 消息的哪一步
    pending: dict[str, int] = {}

    for i, msg in enumerate(messages):
        if msg.role == "assistant":
            for block in msg.tool_uses():
                if block.id in pending:
                    raise InvalidConversation(
                        f"第 {i} 条消息里的 tool_use id 重复: {block.id!r}"
                    )
                pending[block.id] = i

        elif msg.role == "user":
            results = msg.tool_results()

            # 规则 2：孤儿 tool_result
            for r in results:
                if r.tool_use_id not in pending:
                    raise InvalidConversation(
                        f"第 {i} 条 user 消息里的 tool_result 找不到对应的 tool_use: "
                        f"{r.tool_use_id!r}"
                    )

            # 规则 1 + 3：上一条 assistant 的 tool_use 必须在此全部结清
            if pending:
                expected = set(pending)
                got = {r.tool_use_id for r in results}
                if missing := expected - got:
                    raise InvalidConversation(
                        f"第 {i} 条 user 消息缺少 {len(missing)} 个 tool_result: "
                        f"{sorted(missing)}（必须与 tool_use 在同一条消息里配对）"
                    )
                pending.clear()

    if pending:
        raise InvalidConversation(
            f"对话结束时仍有 {len(pending)} 个 tool_use 没有收到 tool_result: "
            f"{sorted(pending)}"
        )
