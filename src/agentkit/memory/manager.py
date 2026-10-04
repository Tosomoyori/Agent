"""记忆管理：上下文装配的**唯一 owner**。

这个类的存在理由是消除歧义。在一个 agent 系统里，「发给模型的消息长什么样」
这件事如果由两处代码各拼一份（比如 memory 拼历史、runtime 再拼 system 提示词），
它们迟早会不一致——通常表现为某天有人在 runtime 里加了一条消息，而 memory 那边
的 token 预算没算上它，于是请求在某次上线后开始偶发地撑爆上下文。

所以：**只有这里决定发给模型的消息序列**。runtime 把 system 提示词和用户输入交给它，
它负责取历史、裁剪、加摘要、装配成一个 :class:`ContextWindow`。

本模块只做「working memory + 持久化」。语义记忆、情节记忆与记忆整合
（consolidation）**刻意没做**——那份设计写在了 ``docs/memory-design.md`` 里，
因为「我知道它是什么、为什么没做」比一个半成品更值得放进作品集。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from ..core.types import Message
from .store import SessionInfo, SessionStore
from .working import ContextWindow, WorkingMemory

__all__ = ["MemoryManager"]


class MemoryManager:
    """装配上下文，并把对话落盘。"""

    def __init__(
        self,
        *,
        store: SessionStore | None = None,
        working: WorkingMemory | None = None,
    ) -> None:
        self.store = store
        self.working = working or WorkingMemory(max_tokens=128_000)

    @classmethod
    def open(
        cls,
        db_path: str | Path,
        *,
        max_context_tokens: int = 128_000,
        reserve_for_output: int = 4_000,
    ) -> MemoryManager:
        """打开（或创建）一个基于 SQLite 的记忆管理器。"""
        return cls(
            store=SessionStore(db_path),
            working=WorkingMemory(
                max_tokens=max_context_tokens, reserve_for_output=reserve_for_output
            ),
        )

    # ------------------------------------------------------------ 装配

    async def build_context(
        self,
        session_id: str | None,
        user_input: str,
        *,
        system: str | None = None,
        extra_history: Sequence[Message] | None = None,
    ) -> ContextWindow:
        """装配这一轮要发给模型的完整消息序列。

        SQLite 的调用被 ``to_thread`` 包住——单条消息虽小，几百个并发 run 叠起来
        照样会把事件循环卡住。同步的存储语义就关在这个边界之内。
        """
        history: list[Message] = []
        if self.store is not None and session_id is not None:
            history = await asyncio.to_thread(self.store.load_messages, session_id)
        if extra_history:
            history = [*history, *extra_history]

        messages = [*history, Message.user(user_input)]
        system_message = Message.system(system) if system else None

        window = await self.working.assemble(messages, system=system_message)
        if window.summary:
            window.messages = self.working.fold_summary(window.messages, system_message)

        # system 消息由这里产出，而不是留给调用方去拼。既然这个类是上下文装配的
        # 唯一 owner，那么「发给模型的消息序列长什么样」就该由它负全责——
        # 否则调用方少拼一次 system，token 预算和实际请求就对不上了。
        if system_message is not None:
            window.messages = [system_message, *window.messages]
        return window

    # ------------------------------------------------------------ 持久化

    async def record(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        run_id: str = "",
    ) -> None:
        """把这一轮产生的消息落盘。没配存储或没给 session_id 时是空操作。"""
        if self.store is None or session_id is None or not messages:
            return
        await asyncio.to_thread(
            self.store.append_messages, session_id, list(messages), run_id=run_id
        )

    async def list_sessions(self) -> list[SessionInfo]:
        if self.store is None:
            return []
        return await asyncio.to_thread(self.store.list_sessions)

    async def delete_session(self, session_id: str) -> bool:
        if self.store is None:
            return False
        return await asyncio.to_thread(self.store.delete_session, session_id)

    async def history(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        if self.store is None:
            return []
        return await asyncio.to_thread(self.store.load_messages, session_id, limit=limit)

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def __enter__(self) -> MemoryManager:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
