"""工作记忆、持久化与上下文装配。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.core.types import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    validate_conversation,
)
from agentkit.memory.manager import MemoryManager
from agentkit.memory.store import SessionStore
from agentkit.memory.working import (
    WorkingMemory,
    estimate_messages_tokens,
    estimate_tokens,
)


class TestTokenEstimation:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_cjk_costs_more_per_character_than_latin(self):
        """中文一个字的信息量比一个英文字母大，token 也更多。"""
        chinese = estimate_tokens("你好世界")
        latin = estimate_tokens("abcd")
        assert chinese > latin

    def test_estimate_includes_role_overhead(self):
        one = estimate_messages_tokens([Message.user("x")])
        two = estimate_messages_tokens([Message.user("x"), Message.user("x")])
        assert two - one > estimate_tokens("x")  # 每条消息有固定开销

    def test_tool_call_and_result_counted(self):
        with_tools = Message.assistant(
            ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"})
        )
        assert estimate_messages_tokens([with_tools]) > estimate_messages_tokens(
            [Message.assistant(TextBlock(text=""))]
        )


class TestWorkingMemoryTrimming:
    async def test_no_trimming_when_within_budget(self):
        memory = WorkingMemory(max_tokens=100_000)
        messages = [Message.user(f"消息 {i}") for i in range(5)]
        window = await memory.assemble(messages)
        assert not window.trimmed
        assert window.dropped_count == 0
        assert len(window.messages) == 5

    async def test_drops_oldest_messages_first(self):
        memory = WorkingMemory(max_tokens=400, reserve_for_output=100)
        messages = [Message.user("内容" * 40) for _ in range(10)]
        messages.append(Message.user("最后的问题"))

        window = await memory.assemble(messages)
        assert window.trimmed
        assert window.dropped_count > 0
        assert window.messages[-1].text() == "最后的问题"

    async def test_last_message_is_never_dropped(self):
        """把用户当前的问题裁掉，模型就只能对着半截上下文编——比超预算更糟。"""
        memory = WorkingMemory(max_tokens=50, reserve_for_output=10)
        messages = [Message.user("很早的长消息" * 100), Message.user("现在的问题")]

        window = await memory.assemble(messages)
        assert window.messages[-1].text() == "现在的问题"

    async def test_tool_call_group_is_dropped_atomically(self):
        """``tool_use`` 与 ``tool_result`` 是原子对，必须整组一起丢。

        丢一半会留下孤儿块，下一次请求直接被 API 拒绝——所以这个测试不仅断言
        「丢了」，还用 :func:`validate_conversation` 断言「丢完之后仍然是合法的」。
        """
        # 预算刻意压得比内容小，好让它真的触发裁剪
        memory = WorkingMemory(max_tokens=300, reserve_for_output=50)
        messages = [
            Message.user("读个文件"),
            Message.assistant(
                ToolUseBlock(id="c1", name="read_file", input={"file_path": "a"})
            ),
            Message.from_tool_results(
                [ToolResultBlock(tool_use_id="c1", content="内容" * 200)]
            ),
            Message.user("再读一个"),
            Message.assistant(
                ToolUseBlock(id="c2", name="read_file", input={"file_path": "b"})
            ),
            Message.from_tool_results(
                [ToolResultBlock(tool_use_id="c2", content="内容" * 200)]
            ),
            Message.user("总结一下"),
        ]

        window = await memory.assemble(messages)

        assert window.trimmed
        # 关键断言：裁剪后仍然是合法序列
        validate_conversation(window.messages)
        assert window.messages[-1].text() == "总结一下"

    async def test_refuses_to_drop_a_group_that_would_leave_nothing(self):
        """如果要丢的那组正好是最后两条，就一条都不能丢。"""
        memory = WorkingMemory(max_tokens=60, reserve_for_output=10)
        messages = [
            Message.assistant(
                ToolUseBlock(id="c1", name="f", input={"x": "很长很长的内容" * 50})
            ),
            Message.from_tool_results(
                [ToolResultBlock(tool_use_id="c1", content="结果" * 50)]
            ),
        ]

        window = await memory.assemble(messages)
        # 要么原样保留（上面超预算），要么丢掉整组——但绝不能只剩孤儿 tool_result
        validate_conversation(window.messages)
        assert window.messages, "不能把消息全丢光"

    async def test_never_produces_an_invalid_sequence(self):
        """随机规模下的不变量检查。"""
        for budget in (100, 200, 400, 800, 1600):
            memory = WorkingMemory(max_tokens=budget, reserve_for_output=20)
            messages = []
            for i in range(12):
                messages.append(Message.user(f"问题 {i}" + "填充" * 20))
                messages.append(
                    Message.assistant(
                        ToolUseBlock(id=f"c{i}", name="f", input={"a": "b"})
                    )
                )
                messages.append(
                    Message.from_tool_results(
                        [ToolResultBlock(tool_use_id=f"c{i}", content="结果" * 20)]
                    )
                )
            messages.append(Message.user("最后"))

            window = await memory.assemble(messages)
            validate_conversation(window.messages), f"预算 {budget} 时产出了非法序列"
            assert window.messages[-1].text() == "最后"

    async def test_reserve_must_be_smaller_than_window(self):
        with pytest.raises(ValueError, match="不该超过总窗口"):
            WorkingMemory(max_tokens=1000, reserve_for_output=1000)

    async def test_input_budget_excludes_reserve(self):
        memory = WorkingMemory(max_tokens=10_000, reserve_for_output=2_000)
        assert memory.input_budget == 8_000


class TestSummarization:
    async def test_summary_replaces_dropped_history(self):
        dropped_seen: list[int] = []

        async def summarizer(messages):
            dropped_seen.append(len(messages))
            return "用户之前让我读了一些文件。"

        memory = WorkingMemory(max_tokens=300, reserve_for_output=50, summarizer=summarizer)
        messages = [Message.user("旧内容" * 50) for _ in range(8)]
        messages.append(Message.user("现在的问题"))

        window = await memory.assemble(messages)
        assert window.summary and dropped_seen

        folded = memory.fold_summary(window, None)
        assert "摘要" in folded[0].text()
        assert folded[-1].text() == "现在的问题"

    async def test_no_summarizer_means_no_summary(self):
        memory = WorkingMemory(max_tokens=300, reserve_for_output=50)
        messages = [Message.user("旧内容" * 50) for _ in range(8)]
        messages.append(Message.user("现在的问题"))

        window = await memory.assemble(messages)
        assert window.summary is None
        assert memory.fold_summary(window, None) == window.messages


class TestSessionStore:
    def test_roundtrip(self, tmp_path: Path):
        store = SessionStore(tmp_path / "s.db")
        try:
            store.append_messages(
                "s1",
                [Message.user("你好"), Message.assistant(TextBlock(text="你好呀"))],
                run_id="run_1",
            )
            loaded = store.load_messages("s1")
            assert [m.role for m in loaded] == ["user", "assistant"]
            assert loaded[1].text() == "你好呀"
        finally:
            store.close()

    def test_tool_blocks_survive_serialization(self, tmp_path: Path):
        """内容块是判别联合，序列化后必须能原样还原。"""
        store = SessionStore(tmp_path / "s.db")
        try:
            original = Message.assistant(
                ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"})
            )
            store.append_messages("s1", [original])
            loaded = store.load_messages("s1")
            assert loaded[0] == original
            assert loaded[0].tool_uses()[0].input == {"file_path": "a.txt"}
        finally:
            store.close()

    def test_sessions_are_isolated(self, tmp_path: Path):
        store = SessionStore(tmp_path / "s.db")
        try:
            store.append_messages("a", [Message.user("给 a 的")])
            store.append_messages("b", [Message.user("给 b 的")])
            assert store.load_messages("a")[0].text() == "给 a 的"
            assert store.load_messages("b")[0].text() == "给 b 的"
        finally:
            store.close()

    def test_appends_keep_order(self, tmp_path: Path):
        store = SessionStore(tmp_path / "s.db")
        try:
            for i in range(5):
                store.append_messages("s1", [Message.user(f"第 {i} 条")])
            loaded = store.load_messages("s1")
            assert [m.text() for m in loaded] == [f"第 {i} 条" for i in range(5)]
        finally:
            store.close()

    def test_limit_returns_the_most_recent(self, tmp_path: Path):
        """``limit`` 要取最近的 N 条，不是最早的 N 条。"""
        store = SessionStore(tmp_path / "s.db")
        try:
            for i in range(10):
                store.append_messages("s1", [Message.user(f"第 {i} 条")])
            loaded = store.load_messages("s1", limit=3)
            assert [m.text() for m in loaded] == ["第 7 条", "第 8 条", "第 9 条"]
        finally:
            store.close()

    def test_list_sessions_reports_counts(self, tmp_path: Path):
        store = SessionStore(tmp_path / "s.db")
        try:
            store.append_messages("s1", [Message.user("a"), Message.user("b")])
            infos = store.list_sessions()
            assert len(infos) == 1
            assert infos[0].id == "s1"
            assert infos[0].message_count == 2
        finally:
            store.close()

    def test_delete_cascades_to_messages(self, tmp_path: Path):
        """删会话要连带删消息，不然会攒下一堆孤儿行。"""
        store = SessionStore(tmp_path / "s.db")
        try:
            store.append_messages("s1", [Message.user("a")])
            assert store.delete_session("s1") is True
            assert store.message_count("s1") == 0
            assert not store.session_exists("s1")
        finally:
            store.close()

    def test_deleting_unknown_session_reports_false(self, tmp_path: Path):
        store = SessionStore(tmp_path / "s.db")
        try:
            assert store.delete_session("不存在") is False
        finally:
            store.close()

    def test_reopening_preserves_data(self, tmp_path: Path):
        path = tmp_path / "s.db"
        store = SessionStore(path)
        store.append_messages("s1", [Message.user("持久化的内容")])
        store.close()

        reopened = SessionStore(path)
        try:
            assert reopened.load_messages("s1")[0].text() == "持久化的内容"
        finally:
            reopened.close()


class TestMemoryManager:
    async def test_build_context_appends_user_input(self, tmp_path: Path):
        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            window = await memory.build_context("s1", "你好", system="你是助手")
            assert window.messages[0].role == "system"
            assert window.messages[-1].text() == "你好"
        finally:
            memory.close()

    async def test_history_carries_across_turns(self, tmp_path: Path):
        """跨轮次记住上下文——这是「记忆」在工程上最直接的含义。"""
        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            await memory.record(
                "s1",
                [Message.user("我叫小明"), Message.assistant(TextBlock(text="记住了"))],
            )
            window = await memory.build_context("s1", "我叫什么？")
            texts = [m.text() for m in window.messages]
            assert "我叫小明" in texts
            assert "记住了" in texts
        finally:
            memory.close()

    async def test_sessions_do_not_leak_into_each_other(self, tmp_path: Path):
        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            await memory.record("a", [Message.user("a 的秘密")])
            window = await memory.build_context("b", "问题")
            assert "a 的秘密" not in [m.text() for m in window.messages]
        finally:
            memory.close()

    async def test_no_session_id_means_no_persistence(self, tmp_path: Path):
        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            await memory.record(None, [Message.user("不该存")])
            assert await memory.list_sessions() == []
        finally:
            memory.close()

    async def test_works_without_a_store(self):
        """没配存储时记忆管理器仍然可用，只是不落盘。"""
        memory = MemoryManager()
        window = await memory.build_context("s1", "你好")
        assert window.messages[-1].text() == "你好"
        await memory.record("s1", [Message.user("x")])  # 空操作
        assert await memory.list_sessions() == []


class TestEngineMemoryIntegration:
    async def test_history_is_replayed_to_the_model(self, tmp_path: Path):
        """第二轮里，模型应当看到第一轮说过的话。"""
        from agentkit.runtime.engine import Engine
        from agentkit.tools.registry import ToolRegistry

        from .conftest import ScriptedModel, assistant_text

        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            model = ScriptedModel(assistant_text("好的"), assistant_text("你叫小明"))
            engine = Engine(
                model, ToolRegistry(), workspace=tmp_path, memory=memory
            )

            async for _ in engine.run("我叫小明", session_id="s1"):
                pass
            async for _ in engine.run("我叫什么", session_id="s1"):
                pass

            second_call = model.calls[1]
            assert "我叫小明" in [m.text() for m in second_call]
        finally:
            memory.close()

    async def test_incomplete_turn_is_not_persisted(self, tmp_path: Path):
        """消息配对不完整时跳过落盘。

        取消或出错时 assistant 的 tool_use 可能还没等到 tool_result，
        存下去会让下一轮一开场就违反不变量、被 API 拒绝。
        """
        from agentkit.runtime.cancellation import CancellationToken
        from agentkit.runtime.engine import Engine
        from agentkit.tools.base import ToolSpec
        from agentkit.tools.registry import ToolRegistry

        from .conftest import ScriptedModel, assistant_tools

        memory = MemoryManager.open(tmp_path / "s.db")
        try:
            registry = ToolRegistry()
            token = CancellationToken()

            async def canceller(ctx, x: str = "") -> str:
                """在工具里取消 run。"""
                token.cancel("取消")
                return "ok"

            registry.add(ToolSpec.from_function(canceller))
            model = ScriptedModel(assistant_tools(("canceller", {})))
            engine = Engine(model, registry, workspace=tmp_path, memory=memory)

            async for _ in engine.run("跑", session_id="s1", cancellation=token):
                pass

            stored = await memory.history("s1")
            # 这一轮被中断，消息配对不完整，所以什么都没存
            assert stored == []
        finally:
            memory.close()
