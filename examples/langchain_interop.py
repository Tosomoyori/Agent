"""LangChain 双向互操作的完整示例。

跑之前先装可选依赖::

    uv sync --extra langchain
    uv run python examples/langchain_interop.py

这个示例不需要 API Key——用的是回放模型。要看真实调用，
把下面的 ``ReplayModel`` 换成 ``agentkit.runtime.build_model(load_settings())`` 即可。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from agentkit.core.types import Message, ReasoningBlock, ToolUseBlock
from agentkit.evaluation.case import ScriptStep
from agentkit.evaluation.replay import ReplayModel
from agentkit.integrations.langchain import (
    AgentKitChatModel,
    from_langchain_tools,
    to_langchain_messages,
    to_langchain_tools,
)
from agentkit.tools.base import ToolContext
from agentkit.tools.builtin import register_builtin_tools


async def main() -> None:
    registry = register_builtin_tools(groups=["fs"])
    workspace = Path.cwd()

    # ------------------------------------------------------------------
    # 方向一：AgentKit 的模型当 LangChain 模型用
    # ------------------------------------------------------------------
    llm = AgentKitChatModel(
        model=ReplayModel(
            [
                ScriptStep(call=("read_file", {"file_path": "README.md"})),
                ScriptStep(answer="README 的第一行是 # AgentKit"),
            ]
        )
    )

    print("1. 作为 LangChain 模型：bind_tools + 多轮工具调用")
    tools = to_langchain_tools(registry, workspace=workspace)
    reply = await llm.bind_tools(tools).ainvoke([HumanMessage("README 第一行是什么？")])

    call = reply.tool_calls[0]
    print(f"   模型决定调用: {call['name']}({call['args']})")

    # 用 AgentKit 自己的注册表执行，再把结果包成 LangChain 的 ToolMessage 喂回去
    ctx = ToolContext(workspace=workspace)
    output = await registry.get(call["name"]).fn(
        ctx, **{k: v for k, v in call["args"].items() if k != "ctx"}
    )
    final = await llm.ainvoke(
        [
            HumanMessage("README 第一行是什么？"),
            reply,
            ToolMessage(content=output[:100], tool_call_id=call["id"]),
        ]
    )
    print(f"   最终答复: {final.content}")

    # ------------------------------------------------------------------
    # 2. 进 LCEL 管道
    # ------------------------------------------------------------------
    print("\n2. 进 LCEL 管道")
    chain = (
        ChatPromptTemplate.from_messages([("system", "你是助手"), ("human", "{q}")])
        | AgentKitChatModel(model=ReplayModel([ScriptStep(answer="管道通了")]))
        | StrOutputParser()
    )
    print(f"   {await chain.ainvoke({'q': '在吗'})}")

    # ------------------------------------------------------------------
    # 3. 消息双向转换
    # ------------------------------------------------------------------
    print("\n3. 消息双向转换（含思维链）")
    internal = [
        Message.user("读个文件"),
        Message.assistant(
            ReasoningBlock(text="我需要先看看文件"),
            ToolUseBlock(id="c1", name="read_file", input={"file_path": "a.txt"}),
        ),
    ]
    converted = to_langchain_messages(internal)
    print(f"   AI 消息的 additional_kwargs: {converted[1].additional_kwargs}")
    print("   （这个键不能丢——DeepSeek 多轮工具调用时要求原样回传，否则 400）")

    # ------------------------------------------------------------------
    # 方向二：LangChain 生态的工具接进 AgentKit
    # ------------------------------------------------------------------
    print("\n4. 方向二：把 LangChain 工具接进 AgentKit 注册表")
    langchain_tools = to_langchain_tools(registry, workspace=workspace)
    imported = from_langchain_tools(langchain_tools, workspace=workspace)
    print(f"   接进来: {imported.names()}")

    result = await imported.get("list_directory").fn(ctx, path=".")
    print(f"   执行结果首行: {result.splitlines()[0]}")


if __name__ == "__main__":
    asyncio.run(main())
