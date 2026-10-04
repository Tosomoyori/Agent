"""工作记忆：在 token 预算内装配上下文。

模型没有记忆——每一轮都要把「它该知道的一切」重新发过去。所以「记忆」在工程上的
真实含义是**上下文装配策略**：在有限的预算里，放哪些、丢哪些、丢掉的信息怎么补偿。

两个机制：

**裁剪**——超出预算时从最早的对话开始丢。这里有个必须守住的约束：
``tool_use`` 和它的 ``tool_result`` 是原子对，要么一起留要么一起丢。
丢掉一半会产生孤儿块，下一次请求直接被 API 拒绝（Anthropic 报得更明确，
OpenAI 系则表现为模型胡言乱语）。:func:`~agentkit.core.types.validate_conversation`
用同一套不变量做校验，两边是配套的。

**摘要压缩**——直接丢掉早期对话会永久损失信息。所以被裁掉的部分交给模型总结成
一段话，作为一条 ``system`` 消息保留在开头。这是 Anthropic 在生产环境里的做法，
也是本项目里唯一一处「用模型解决模型的问题」。

**保底不变量**：无论预算多紧，**最后一条 user 消息必须留下**。把用户当前的问题
裁掉，模型就会对着半截上下文开始编——这比超出预算更糟。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ..core.types import Message, TextBlock

__all__ = [
    "WorkingMemory",
    "ContextWindow",
    "estimate_tokens",
    "estimate_messages_tokens",
]

#: 摘要器的签名：吃一段被裁掉的消息，吐一段摘要。
Summarizer = Callable[[Sequence[Message]], Awaitable[str]]


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数。

    没有官方离线 tokenizer 可用（DeepSeek 提供了一个 demo 版，但把它打进依赖
    不值得），所以按字符估算：CJK 字符约 0.6 token/字，其余约 0.3 token/字符
    （这两个系数来自官方文档的粗略指引）。

    精度足够做**预算裁剪**——裁剪本来就该留安全余量，算多了只是少放几条历史，
    算少了才会撑爆请求。真正的精确计数只在计费时有意义，而计费一律以服务端
    返回的 ``usage`` 为准，不用估算值。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return int(cjk * 0.6 + (len(text) - cjk) * 0.3) + 1


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # 基本汉字
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角字符
    )


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """估算一组消息占用的 token。

    每条消息额外加一点固定开销，对应 role 标记和结构分隔符——不算这部分的话，
    很多条短消息的场景会被系统性低估。
    """
    total = 0
    for message in messages:
        total += 4  # role 与分隔符的近似开销
        for block in message.content:
            total += estimate_tokens(_block_text(block))
        for use in message.tool_uses():
            total += estimate_tokens(use.name) + estimate_tokens(str(use.input))
    return total


def _block_text(block) -> str:
    from ..core.types import ReasoningBlock, TextBlock, ToolResultBlock

    if isinstance(block, TextBlock | ReasoningBlock):
        return block.text
    if isinstance(block, ToolResultBlock):
        return block.content
    return ""


@dataclass
class ContextWindow:
    """装配好的上下文，附带它是怎么来的。"""

    messages: list[Message] = field(default_factory=list)
    #: 被裁掉的消息条数。
    dropped_count: int = 0
    #: 被裁掉部分的摘要（如果有摘要器）。
    summary: str | None = None
    #: 估算的 token 占用。
    estimated_tokens: int = 0
    #: 是否因为预算不足而裁剪过。
    trimmed: bool = False

    def __len__(self) -> int:
        return len(self.messages)


class WorkingMemory:
    """在 token 预算内装配上下文。"""

    def __init__(
        self,
        max_tokens: int,
        *,
        reserve_for_output: int = 4_000,
        summarizer: Summarizer | None = None,
    ) -> None:
        """
        :param max_tokens: 模型上下文窗口大小。
        :param reserve_for_output: 给模型回复留的余量。不留的话，一份塞得满满的
            上下文会让模型没有空间输出，直接报错。
        :param summarizer: 可选。用来把裁掉的历史总结成一段话。
        """
        if reserve_for_output >= max_tokens:
            raise ValueError(
                f"给输出预留的 {reserve_for_output} 不该超过总窗口 {max_tokens}"
            )
        self.max_tokens = max_tokens
        self.reserve_for_output = reserve_for_output
        self.summarizer = summarizer

    @property
    def input_budget(self) -> int:
        """可用于输入消息的 token 预算。"""
        return self.max_tokens - self.reserve_for_output

    async def assemble(
        self,
        messages: Sequence[Message],
        *,
        system: Message | None = None,
    ) -> ContextWindow:
        """按预算裁剪消息，必要时对裁掉的部分做摘要。"""
        kept = list(messages)
        # system 提示词不参与裁剪——它是行为约束，丢了模型就不守规矩了
        overhead = estimate_messages_tokens([system]) if system else 0
        budget = max(0, self.input_budget - overhead)

        if estimate_messages_tokens(kept) <= budget:
            window = ContextWindow(messages=kept)
            window.estimated_tokens = estimate_messages_tokens(kept) + overhead
            return window

        dropped: list[Message] = []
        while kept and estimate_messages_tokens(kept) > budget:
            group = _take_droppable_prefix(kept)
            if group is None:
                break  # 只剩最后一条 user 消息了，不能再丢
            dropped.extend(group)

        summary = None
        if dropped and self.summarizer is not None:
            summary = await self.summarizer(dropped)

        window = ContextWindow(
            messages=kept,
            dropped_count=len(dropped),
            summary=summary,
            trimmed=True,
        )
        window.estimated_tokens = estimate_messages_tokens(kept) + overhead
        if summary:
            window.estimated_tokens += estimate_tokens(summary)
        return window

    def fold_summary(self, window: ContextWindow, system: Message | None) -> list[Message]:
        """把摘要插到 system 之后、历史之前。

        位置是刻意的：摘要属于**背景**，不是最新的一轮对话。放到后面会让模型
        把总结出来的旧信息当成刚发生的事。
        """
        if not window.summary:
            return window.messages

        summary_message = Message(
            role="system",
            content=[
                TextBlock(
                    text=f"以下是更早对话的摘要（原文已因上下文长度被裁剪）：\n{window.summary}"
                )
            ],
        )
        return [summary_message, *window.messages]


def _take_droppable_prefix(messages: list[Message]) -> list[Message] | None:
    """从消息列表开头摘掉一组可以安全丢弃的消息，就地修改并返回被摘掉的部分。

    两条硬规则：

    * **工具调用组必须整组一起丢**。``assistant`` 的 ``tool_use`` 与其后 ``user``
      的 ``tool_result`` 是一个原子对，丢一半会留下孤儿块，下一次请求直接被
      API 拒绝。所以「只能丢一半」的时候，答案是**一半都不丢**。
    * **至少留下一条消息**。它通常是用户当前的问题，裁掉它模型就只能对着半截
      上下文编，比超预算更糟。

    返回 ``None`` 表示已经没有可丢的了。
    """
    if len(messages) <= 1:
        return None

    head = messages[0]

    if head.role == "assistant" and head.tool_uses():
        follower = messages[1] if len(messages) > 1 else None
        is_tool_group = (
            follower is not None
            and follower.role == "user"
            and bool(follower.tool_results())
        )

        if not is_tool_group:
            # 带 tool_use 却没有配对结果——数据本来就坏了，只丢它自己即可
            return [messages.pop(0)]

        # 整组丢掉之后至少要剩一条，否则这一组不能动
        if len(messages) < 3:
            return None
        return [messages.pop(0), messages.pop(0)]

    # 普通消息：只丢它自己
    return [messages.pop(0)]
