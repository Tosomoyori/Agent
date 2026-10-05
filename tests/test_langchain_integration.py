"""LangChain 互操作层。

用 ``importorskip`` 保护：LangChain 是可选的 extra（``uv sync --extra langchain``），
没装它的人跑测试套件不该因此失败。
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="需要 uv sync --extra langchain")

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402

from agentkit.core.types import (  # noqa: E402
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    validate_conversation,
)
from agentkit.core.usage import Usage  # noqa: E402
from agentkit.evaluation.case import ScriptStep  # noqa: E402
from agentkit.evaluation.replay import ReplayModel  # noqa: E402
from agentkit.integrations.langchain import (  # noqa: E402
    AgentKitChatModel,
    from_langchain_messages,
    from_langchain_tools,
    to_langchain_messages,
    to_langchain_tools,
)
from agentkit.tools.builtin import register_builtin_tools  # noqa: E402

# ================================================================ 消息转换


class TestToLangChain:
    def test_system_and_user(self):
        result = to_langchain_messages([Message.system("sys"), Message.user("你好")])
        assert isinstance(result[0], SystemMessage)
        assert isinstance(result[1], HumanMessage)

    def test_tool_results_become_tool_messages(self):
        """AgentKit 里工具结果属于 user 消息的 content block，LangChain 用独立消息。"""
        result = to_langchain_messages(
            [
                Message.from_tool_results(
                    [
                        ToolResultBlock(tool_use_id="c1", content="甲"),
                        ToolResultBlock(tool_use_id="c2", content="乙", is_error=True),
                    ]
                )
            ]
        )
        assert [type(m).__name__ for m in result] == ["ToolMessage", "ToolMessage"]
        assert result[0].tool_call_id == "c1"
        assert result[1].status == "error"

    def test_tool_use_becomes_tool_calls(self):
        result = to_langchain_messages(
            [
                Message.assistant(
                    ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"})
                )
            ]
        )
        call = result[0].tool_calls[0]
        assert call["name"] == "read_file"
        assert call["args"] == {"file_path": "a.txt"}
        assert call["id"] == "c1"

    def test_reasoning_goes_into_additional_kwargs(self):
        """**这条是关键**：丢了它，DeepSeek 多轮工具调用会返回 400。"""
        result = to_langchain_messages(
            [Message.assistant(ReasoningBlock(text="我在想"), TextBlock(text="答案"))]
        )
        assert result[0].additional_kwargs["reasoning_content"] == "我在想"
        assert result[0].content == "答案"


class TestFromLangChain:
    def test_basic_roles(self):
        result = from_langchain_messages(
            [SystemMessage(content="s"), HumanMessage(content="u"), AIMessage(content="a")]
        )
        assert [m.role for m in result] == ["system", "user", "assistant"]
        assert result[2].text() == "a"

    def test_consecutive_tool_messages_are_merged(self):
        """**这是反向转换里唯一有结构难度的地方。**

        LangChain 里每条工具结果是一条独立的 ``ToolMessage``，而 AgentKit 要求
        同一轮的结果合并在**一条** user 消息里。不合并的话，下一个请求会因为
        tool_use 找不到配对的 tool_result 被拒绝。
        """
        result = from_langchain_messages(
            [
                HumanMessage(content="做两件事"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "a", "args": {}, "id": "c1", "type": "tool_call"},
                        {"name": "b", "args": {}, "id": "c2", "type": "tool_call"},
                    ],
                ),
                ToolMessage(content="结果甲", tool_call_id="c1"),
                ToolMessage(content="结果乙", tool_call_id="c2"),
            ]
        )

        # 两条 ToolMessage 合并成了一条 user 消息
        assert [m.role for m in result] == ["user", "assistant", "user"]
        assert len(result[-1].tool_results()) == 2
        validate_conversation(result)  # 不抛异常即通过

    def test_reasoning_is_restored(self):
        result = from_langchain_messages(
            [
                AIMessage(
                    content="答案",
                    additional_kwargs={"reasoning_content": "我在想"},
                )
            ]
        )
        assert result[0].reasoning_text() == "我在想"

    def test_content_block_list_is_flattened(self):
        """LangChain 的 content 可能是字符串，也可能是 block 列表。"""
        result = from_langchain_messages(
            [HumanMessage(content=[{"type": "text", "text": "分块"}]  )]
        )
        assert result[0].text() == "分块"


class TestMessageRoundTrip:
    def test_pairing_invariant_survives(self):
        """往返一圈之后，配对不变量必须仍然成立。

        这是整个适配层最重要的一条性质——不成立的话，转一圈回来就被 API 拒绝。
        """
        original = [
            Message.system("你是助手"),
            Message.user("读两个文件"),
            Message.assistant(
                ReasoningBlock(text="想想"),
                ToolUseBlock(id="c1", name="read_file", input={"file_path": "a"}),
                ToolUseBlock(id="c2", name="read_file", input={"file_path": "b"}),
            ),
            Message.from_tool_results(
                [
                    ToolResultBlock(tool_use_id="c1", content="甲"),
                    ToolResultBlock(tool_use_id="c2", content="乙", is_error=True),
                ]
            ),
            Message.assistant(TextBlock(text="都读完了")),
        ]

        restored = from_langchain_messages(to_langchain_messages(original))
        validate_conversation(restored)

        assert [m.role for m in restored] == [m.role for m in original]
        assert restored[2].reasoning_text() == "想想"
        assert len(restored[2].tool_uses()) == 2
        assert restored[3].tool_results()[1].is_error is True


# ================================================================ 模型包装


def _llm(*steps: ScriptStep) -> AgentKitChatModel:
    return AgentKitChatModel(model=ReplayModel(list(steps)))


class TestChatModelWrapper:
    async def test_ainvoke(self):
        reply = await _llm(ScriptStep(answer="你好呀")).ainvoke([HumanMessage("在吗")])
        assert reply.content == "你好呀"

    async def test_usage_metadata(self):
        reply = await _llm(ScriptStep(answer="x")).ainvoke([HumanMessage("x")])
        assert reply.usage_metadata["input_tokens"] == 100
        assert reply.usage_metadata["output_tokens"] == 20
        assert reply.usage_metadata["total_tokens"] == 120

    async def test_llm_type(self):
        assert _llm(ScriptStep(answer="x"))._llm_type == "agentkit"

    def test_sync_invoke_outside_a_loop(self):
        assert _llm(ScriptStep(answer="同步")).invoke([HumanMessage("x")]).content == "同步"

    async def test_sync_invoke_inside_a_running_loop(self):
        """从异步代码里调同步 API 是常见用法，不该炸一个看不懂的 RuntimeError。

        裸 ``asyncio.run`` 会抛 ``cannot be called from a running event loop``。
        实现里检测到已有循环就丢到另一个线程去跑。
        """
        llm = _llm(ScriptStep(answer="也能跑"))
        assert llm.invoke([HumanMessage("x")]).content == "也能跑"

    async def test_astream_yields_incremental_chunks(self):
        """不实现 ``_astream`` 的话 LangChain 会退化成「等完再一次性吐」。"""
        got = [
            chunk.content
            async for chunk in _llm(ScriptStep(answer="逐字")).astream(
                [HumanMessage("x")]
            )
            if chunk.content
        ]
        assert "".join(got) == "逐字"


class TestBindTools:
    async def test_bind_tools_is_implemented(self):
        """``BaseChatModel.bind_tools`` 默认是 ``raise NotImplementedError``。

        不覆写它的话，``llm.bind_tools(...)``——LangChain agent 和 LCEL 里用工具的
        标准入口——会直接抛错。
        """
        tools = to_langchain_tools(register_builtin_tools(groups=["fs"]))
        bound = _llm(ScriptStep(call=("read_file", {"file_path": "a.txt"}))).bind_tools(tools)

        reply = await bound.ainvoke([HumanMessage("读一下")])
        assert reply.tool_calls[0]["name"] == "read_file"
        assert reply.tool_calls[0]["args"] == {"file_path": "a.txt"}

    async def test_bound_tool_schemas_reach_the_underlying_model(self):
        """绑定的工具要真的传到 AgentKit 的模型上，而不是被悄悄丢掉。"""
        seen: list[list | None] = []

        class Spy(ReplayModel):
            def stream(self, *, messages, tools=None, tool_choice="auto", **kwargs):
                seen.append(tools)
                return super().stream(
                    messages=messages, tools=tools, tool_choice=tool_choice, **kwargs
                )

        llm = AgentKitChatModel(
            model=Spy([ScriptStep(call=("read_file", {"file_path": "a"}))])
        )
        tools = to_langchain_tools(register_builtin_tools(groups=["fs"]))
        await llm.bind_tools(tools).ainvoke([HumanMessage("x")])

        assert seen and seen[0] is not None
        assert "read_file" in [schema.name for schema in seen[0]]


class TestLCEL:
    async def test_pipe_chain(self):
        """能进 LCEL 管道是这个包装器的核心价值。"""
        chain = (
            ChatPromptTemplate.from_messages(
                [("system", "你是助手"), ("human", "{q}")]
            )
            | _llm(ScriptStep(answer="管道通了"))
            | StrOutputParser()
        )
        assert await chain.ainvoke({"q": "在吗"}) == "管道通了"


# ================================================================ 工具转换


class TestToolConversion:
    def test_registry_to_langchain(self):
        tools = to_langchain_tools(register_builtin_tools(groups=["fs"]))
        names = {t.name for t in tools}
        assert names == {"read_file", "write_file", "list_directory"}

    def test_context_parameter_is_stripped(self):
        """AgentKit 的工具第一个参数是注入的 ``ToolContext``，模型看不到它。

        转成 LangChain 工具时必须剥掉——LangChain 没有上下文注入这个概念。
        """
        tools = to_langchain_tools(register_builtin_tools(groups=["fs"]))
        read = next(t for t in tools if t.name == "read_file")
        assert "ctx" not in read.args_schema.model_fields
        assert "file_path" in read.args_schema.model_fields

    def test_langchain_to_registry(self):
        registry = from_langchain_tools(
            to_langchain_tools(register_builtin_tools(groups=["fs"]))
        )
        assert set(registry.names()) == {"read_file", "write_file", "list_directory"}

    def test_imported_tools_are_marked_not_idempotent_by_default(self):
        """来路不明的第三方工具，保守假设它不幂等。

        幂等标记会影响将来重放时能不能跳过——对一个不知道内部干了什么的工具
        假设它幂等，是在拿正确性赌。
        """
        registry = from_langchain_tools(
            to_langchain_tools(register_builtin_tools(groups=["fs"]))
        )
        assert all(not spec.idempotent for spec in registry.all())

    def test_dangerous_defaults_to_false(self):
        """``dangerous`` 会影响要不要走审批，默认说第三方工具安全是不负责任的。"""
        registry = from_langchain_tools(
            to_langchain_tools(register_builtin_tools(groups=["fs"]))
        )
        assert all(not spec.dangerous for spec in registry.all())

    def test_dangerous_can_be_forced(self):
        registry = from_langchain_tools(
            to_langchain_tools(register_builtin_tools(groups=["fs"])), dangerous=True
        )
        assert all(spec.dangerous for spec in registry.all())

    async def test_imported_tool_actually_runs(self, workspace):
        """转一圈回来还要能真的执行——只测 schema 是不够的。

        注意两个方向都要传 ``workspace``：往外转时它被固定进工具内部，
        往里转时的那个参数管不到内层。
        """
        registry = from_langchain_tools(
            to_langchain_tools(register_builtin_tools(groups=["fs"]), workspace=workspace),
            workspace=workspace,
        )
        from agentkit.tools.base import ToolContext

        ctx = ToolContext(workspace=workspace)
        result = await registry.get("read_file").fn(ctx, file_path="hello.txt")
        assert "第一行" in result

    async def test_workspace_is_fixed_at_conversion_time(self, tmp_path):
        """往外转时固定的工作区，**不会**被往里转时传的目录覆盖。

        这条固定的是一个容易踩的行为：两个方向各传一个 workspace 时，
        内层工具仍然指向**往外转时**的那个。测试不是在赞美这个设计，
        而是把它钉住——文档里写清楚了，行为就不能悄悄变。
        """
        from agentkit.tools.base import ToolContext

        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "f.txt").write_text("来自外层\n", encoding="utf-8")

        inner = tmp_path / "inner"
        inner.mkdir()
        (inner / "f.txt").write_text("来自内层\n", encoding="utf-8")

        tools = to_langchain_tools(
            register_builtin_tools(groups=["fs"]), workspace=outer
        )
        restored = from_langchain_tools(tools, workspace=inner)

        # 调用时给的上下文指向 inner，但工具在转换时就绑定了 outer
        ctx = ToolContext(workspace=inner)
        result = await restored.get("read_file").fn(ctx, file_path="f.txt")
        assert "来自外层" in result


# ================================================================ 用法示例


class TestEndToEndUsage:
    async def test_agentkit_model_as_a_langchain_model(self, workspace):
        """完整用法：AgentKit 的模型 + 工具，接进 LangChain 的管道。"""
        from agentkit.tools.base import ToolContext

        registry = register_builtin_tools(groups=["fs"])
        tools = to_langchain_tools(registry)
        llm = _llm(
            ScriptStep(call=("read_file", {"file_path": "hello.txt"})),
            ScriptStep(answer="文件有三行"),
        )

        reply = await llm.bind_tools(tools).ainvoke(
            [HumanMessage("hello.txt 里有几行？")]
        )
        call = reply.tool_calls[0]
        assert call["name"] == "read_file"

        # 用 AgentKit 的注册表真正执行这次调用，再把结果包成 ToolMessage 喂回去
        ctx = ToolContext(workspace=workspace)
        output = await registry.get(call["name"]).fn(ctx, **call["args"])

        final = await llm.ainvoke(
            [
                HumanMessage("hello.txt 里有几行？"),
                reply,
                ToolMessage(content=output, tool_call_id=call["id"]),
            ]
        )
        assert final.content == "文件有三行"

    def test_usage_mapping_is_lossless(self):
        """两边的 usage 是同一套子集语义，字段名不同但可以无损映射。"""
        from agentkit.integrations.langchain import _usage_metadata

        usage = Usage(
            input_tokens=1000,
            output_tokens=200,
            cached_input_tokens=800,
            reasoning_tokens=50,
        )
        metadata = _usage_metadata(usage)
        assert metadata["input_tokens"] == 1000
        assert metadata["input_token_details"]["cache_read"] == 800
        assert metadata["output_token_details"]["reasoning"] == 50
        # 子集语义一致：缓存是输入的一部分，不是额外的
        assert metadata["total_tokens"] == 1200
