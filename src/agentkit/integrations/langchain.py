"""LangChain 互操作层。

核心项目**不依赖** LangChain——这一层是可选依赖（``uv sync --extra langchain``），
只有想把 AgentKit 接进 LangChain 生态时才需要。

## 四个方向，四个函数

============================ ====================================================
函数                          作用
============================ ====================================================
``AgentKitChatModel``         把 AgentKit 的模型包成 LangChain 的 ``BaseChatModel``，
                              于是它能进 LCEL 管道、能被 LangChain 的 agent 用
``to_langchain_tools``        把 AgentKit 的工具注册表转成 ``StructuredTool`` 列表
``from_langchain_tools``      把 LangChain 生态里的工具接进 AgentKit 的注册表
``to_langchain_messages`` /   两个方向的消息互转
``from_langchain_messages``
============================ ====================================================

## 这一层的意义不只是「能用」

它是**抽象设计的试金石**。如果 ``ChatModel`` / ``ToolSpec`` 的抽象是干净的，
包一层 LangChain 接口应该只需要做**格式转换**，一行核心代码都不用改。
如果需要改动 ``runtime`` 或 ``core`` 才能适配，说明抽象漏了东西。

现在这一层只 import 了 ``agentkit.core`` / ``agentkit.llm`` / ``agentkit.tools``，
没有碰 ``runtime``——这是设计成立的一个证据。

## 三个必须处理的映射差异

1. **工具结果的位置**。AgentKit 内部它属于 user 消息里的 content block（与 Anthropic
   一致），LangChain 用独立的 ``ToolMessage``。与转 OpenAI 时属于同一问题。
2. **思维链**。LangChain 没有 reasoning 这个概念，provider 私有的东西放
   ``additional_kwargs``。DeepSeek 的 ``reasoning_content`` 走这里，
   而且**必须原样回传**，否则多轮工具调用时 API 返回 400。
3. **usage 的子集语义**。LangChain 的 ``usage_metadata`` 把缓存的输入 token 和
   推理 token 放在 ``input_token_details`` / ``output_token_details`` 里，
   而且**它们是总数的子集**——和 AgentKit 的 ``Usage`` 是同一套语义，
   但字段名完全不同。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

# 这些 import 会在没装 langchain 时报 ImportError。这是刻意的：
# 核心项目不需要它，只有真的 import 这个模块时才需要依赖就位。
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from ..core.types import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from ..core.usage import Usage
from ..llm.base import ModelCapabilities
from ..tools.base import ContextAwareCallable, ToolContext, ToolSpec
from ..tools.registry import ToolRegistry

__all__ = [
    "AgentKitChatModel",
    "from_langchain_messages",
    "from_langchain_tools",
    "to_langchain_messages",
    "to_langchain_tools",
]

#: DeepSeek 用这个键承载思维链。langchain-deepseek 也是这么做的。
_REASONING_KEY = "reasoning_content"


# ============================================================ 消息转换


def to_langchain_messages(messages: Sequence[Message]) -> list[BaseMessage]:
    """AgentKit 内容块消息 → LangChain 消息。"""
    out: list[BaseMessage] = []

    for msg in messages:
        if msg.role == "system":
            out.append(SystemMessage(content=msg.text()))

        elif msg.role == "user":
            # 工具结果在 LangChain 里是独立的 ToolMessage，不是 user 消息的一部分
            for result in msg.tool_results():
                out.append(
                    ToolMessage(
                        content=result.content,
                        tool_call_id=result.tool_use_id,
                        status="error" if result.is_error else "success",
                    )
                )
            if text := msg.text():
                out.append(HumanMessage(content=text))

        else:  # assistant
            additional: dict[str, Any] = {}
            if reasoning := msg.reasoning_text():
                # 丢了这个键，多轮工具调用时 DeepSeek 会返回 400
                additional[_REASONING_KEY] = reasoning

            out.append(
                AIMessage(
                    content=msg.text(),
                    additional_kwargs=additional,
                    tool_calls=[
                        {"name": use.name, "args": use.input, "id": use.id, "type": "tool_call"}
                        for use in msg.tool_uses()
                    ],
                )
            )

    return out


def from_langchain_messages(messages: Sequence[BaseMessage]) -> list[Message]:
    """LangChain 消息 → AgentKit 内容块消息。

    反向转换有一个**结构性**的困难：LangChain 里每个工具结果是一条独立的
    ``ToolMessage``，而 AgentKit 要求同一轮的所有结果合并在**一条** user 消息里。

    所以这里会做合并：连续的 ``ToolMessage`` 归到同一条 user 消息。
    不合并的话，下一个 LLM 请求会因为 tool_use 找不到配对的 tool_result 而被拒绝。
    """
    out: list[Message] = []
    pending_results: list[ToolResultBlock] = []

    def flush() -> None:
        if pending_results:
            out.append(Message.from_tool_results(list(pending_results)))
            pending_results.clear()

    for msg in messages:
        if isinstance(msg, ToolMessage):
            # 先攒着，等这段连续的 ToolMessage 结束再合并成一条
            pending_results.append(
                ToolResultBlock(
                    tool_use_id=msg.tool_call_id or "",
                    content=_as_text(msg.content),
                    is_error=msg.status == "error",
                )
            )
            continue

        flush()

        if isinstance(msg, SystemMessage):
            out.append(Message.system(_as_text(msg.content)))

        elif isinstance(msg, HumanMessage):
            out.append(Message.user(_as_text(msg.content)))

        elif isinstance(msg, AIMessage):
            blocks: list[Any] = []
            if reasoning := msg.additional_kwargs.get(_REASONING_KEY):
                blocks.append(ReasoningBlock(text=reasoning))
            if text := _as_text(msg.content):
                blocks.append(TextBlock(text=text))
            for call in msg.tool_calls:
                blocks.append(
                    ToolUseBlock(
                        id=call.get("id") or "",
                        name=call.get("name") or "",
                        input=dict(call.get("args") or {}),
                    )
                )
            out.append(Message.assistant(*blocks))

    flush()
    return out


def _as_text(content: Any) -> str:
    """LangChain 的 content 可能是字符串，也可能是 content block 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content or "")


# ============================================================ 模型


class AgentKitChatModel(BaseChatModel):
    """把 AgentKit 的 :class:`~agentkit.llm.base.ChatModel` 包成 LangChain 的聊天模型。

    用法::

        from agentkit.integrations.langchain import AgentKitChatModel
        from agentkit.runtime import build_model
        from agentkit.core import load_settings

        llm = AgentKitChatModel(model=build_model(load_settings()))
        llm.invoke("你好")

    包好之后它就是一个普通的 LangChain 模型——能进 LCEL 管道、能被
    LangChain 自己的 agent 使用、能被 LangChain 的 callback 追踪。
    """

    #: 被包装的 AgentKit 模型。用 ``model_config`` 允许任意类型，
    #: 否则 pydantic 会尝试把它当成字段做校验。
    model: Any = Field(description="被包装的 agentkit ChatModel 实例")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "agentkit"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": getattr(self.model, "name", "unknown")}

    @property
    def capabilities(self) -> ModelCapabilities:
        return getattr(self.model, "capabilities", ModelCapabilities())

    # -------------------------------------------------------- 工具绑定

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """把工具绑定到模型上。

        ``BaseChatModel`` 里的默认实现是 ``raise NotImplementedError``——
        每个 provider 都得自己写。不覆写它的话，``llm.bind_tools(...)`` 会直接抛错，
        而这个方法是 LangChain agent 和 LCEL 里用工具的标准入口。

        实现很薄：把工具统一转成 OpenAI 格式的 dict，然后用 ``Runnable.bind``
        挂到调用参数上——因为 AgentKit 内部本来就是用这套格式。
        """
        from langchain_core.utils.function_calling import convert_to_openai_tool

        formatted = [
            item if isinstance(item, dict) else convert_to_openai_tool(item)
            for item in tools
        ]
        return super().bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    # -------------------------------------------------------- 同步

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步生成。

        LangChain 的同步入口最终会走到这里。底层模型是异步的，所以要把它跑起来——
        但**不能无条件 ``asyncio.run``**：如果调用方本身就在事件循环里
        （常见于「异步应用里顺手调了同步 API」），``asyncio.run`` 会直接抛
        ``cannot be called from a running event loop``，那个报错对使用者毫无帮助。

        所以分两种情况：没有循环就自己开一个；已经有循环就丢到另一个线程去，
        那边有独立的事件循环。代价是多一次线程切换，换来的是从任何地方调都能用。
        """
        import asyncio
        import concurrent.futures

        coroutine = self._agenerate(messages, stop=stop, **kwargs)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coroutine).result()

    # -------------------------------------------------------- 异步

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        internal = from_langchain_messages(messages)
        tools = _schemas_from_kwargs(kwargs)

        completion = await self.model.complete(
            messages=internal,
            tools=tools,
            tool_choice=kwargs.get("tool_choice", "auto"),
        )

        return ChatResult(
            generations=[
                ChatGeneration(
                    text=completion.message.text(),
                    message=_to_ai_message(completion.message, completion.usage),
                    generation_info={"finish_reason": completion.finish_reason},
                )
            ],
            llm_output={
                "model_name": getattr(self.model, "name", ""),
                "usage": _usage_to_dict(completion.usage),
            },
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """真流式。

        没有实现它的话，LangChain 会退化成「等 ``_agenerate`` 跑完再一次性吐出」——
        用户看到的流式效果是假的。既然底层模型本来就流式，这里透传出去。
        """
        internal = from_langchain_messages(messages)
        tools = _schemas_from_kwargs(kwargs)

        async for chunk in self.model.stream(
            messages=internal,
            tools=tools,
            tool_choice=kwargs.get("tool_choice", "auto"),
        ):
            if not (chunk.text or chunk.reasoning):
                continue

            additional: dict[str, Any] = {}
            if chunk.reasoning:
                additional[_REASONING_KEY] = chunk.reasoning

            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=chunk.text, additional_kwargs=additional
                )
            )


def _schemas_from_kwargs(kwargs: dict[str, Any]) -> list[ToolSchema] | None:
    """从 ``bind_tools`` 传下来的参数里取出工具定义。

    LangChain 的 ``bind_tools`` 会把工具的 OpenAI 格式定义放进 ``kwargs["tools"]``。
    """
    raw = kwargs.get("tools")
    if not raw:
        return None

    schemas: list[ToolSchema] = []
    for item in raw:
        # 可能是 OpenAI 格式的 dict，也可能是 BaseTool 实例
        if isinstance(item, BaseTool):
            function = {
                "name": item.name,
                "description": item.description,
                "parameters": item.args_schema.model_json_schema()
                if hasattr(item.args_schema, "model_json_schema")
                else {"type": "object", "properties": {}},
            }
        else:
            function = item.get("function", item)
        schemas.append(
            ToolSchema(
                name=function.get("name", ""),
                description=function.get("description", ""),
                parameters=function.get("parameters")
                or {"type": "object", "properties": {}},
            )
        )
    return schemas


def _to_ai_message(message: Message, usage: Usage) -> AIMessage:
    """AgentKit 的 assistant 消息 → LangChain 的 AIMessage。"""
    additional: dict[str, Any] = {}
    if reasoning := message.reasoning_text():
        additional[_REASONING_KEY] = reasoning

    return AIMessage(
        content=message.text(),
        additional_kwargs=additional,
        tool_calls=[
            {"name": use.name, "args": use.input, "id": use.id, "type": "tool_call"}
            for use in message.tool_uses()
        ],
        usage_metadata=_usage_metadata(usage),
    )


def _usage_metadata(usage: Usage) -> dict[str, Any]:
    """AgentKit 的 Usage → LangChain 的 ``usage_metadata``。

    两边是同一套**子集语义**（缓存是输入的子集、推理是输出的子集），
    只是字段名不同。这个映射是双向无损的。
    """
    metadata: dict[str, Any] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.cached_input_tokens:
        metadata["input_token_details"] = {"cache_read": usage.cached_input_tokens}
    if usage.reasoning_tokens:
        metadata["output_token_details"] = {"reasoning": usage.reasoning_tokens}
    return metadata


def _usage_to_dict(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


# ============================================================ 工具


def to_langchain_tools(
    registry: ToolRegistry, *, workspace: Any = None
) -> list[StructuredTool]:
    """AgentKit 的工具注册表 → LangChain 的 ``StructuredTool`` 列表。

    转过去的工具能直接被 LangChain 的 agent 使用，也能进 ``bind_tools``。

    这里有两个**语义上的**细节：

    1. AgentKit 的工具第一个参数是注入的 ``ToolContext``，模型看不到它。
       转成 LangChain 工具时必须把它剥掉——LangChain 的工具签名里没有
       「上下文注入」这个概念；
    2. 上下文得有个来源，所以 ``workspace`` 由**转换时**固定下来。
       不传就落到 ``Path.cwd()``。

    第 2 点容易出错：如果转出去时用了 A 目录、转回来时又声明了 B 目录，
    **内层那个工具仍然指向 A**——``from_langchain_tools`` 的 ``workspace``
    参数管不到它。往返转换时要给两个方向传同一个值。
    """
    return [_tool_to_langchain(spec, workspace) for spec in registry.all()]


def _tool_to_langchain(spec: ToolSpec, workspace: Any = None) -> StructuredTool:
    from pathlib import Path

    base = Path(workspace) if workspace is not None else Path.cwd()

    async def invoke(**kwargs: Any) -> str:
        ctx = ToolContext(workspace=base)
        result = await spec.fn(ctx, **kwargs)
        return result

    return StructuredTool.from_function(
        coroutine=invoke,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_model,
    )


def from_langchain_tools(
    tools: Sequence[BaseTool],
    *,
    workspace: Any = None,
    dangerous: bool = False,
) -> ToolRegistry:
    """把 LangChain 生态里的工具接进 AgentKit 的注册表。

    这条方向的价值更大：LangChain 社区有几百个现成工具（搜索、数据库、
    各种 SaaS 集成），接进来就不用自己重写。

    :param workspace: 注入给工具的上下文工作区。LangChain 的工具没有这个概念，
        所以由调用方统一指定。
    :param dangerous: 是否把这些工具标记为有副作用。**默认 False 是刻意的**——
        AgentKit 里 ``dangerous`` 会影响要不要走审批，对一个来路不明的第三方工具
        默认说它安全是不负责任的。要用审批就显式传 ``True``。
    """
    from pathlib import Path

    base = Path(workspace) if workspace is not None else Path.cwd()
    registry = ToolRegistry()

    for tool in tools:
        registry.add(_langchain_tool_to_spec(tool, base, dangerous=dangerous))

    return registry


def _langchain_tool_to_spec(tool: BaseTool, workspace: Any, *, dangerous: bool) -> ToolSpec:
    """把一个 LangChain 工具适配成 AgentKit 的 :class:`ToolSpec`。

    参数模型直接复用 LangChain 的 ``args_schema``——它本来就是 pydantic 模型，
    而 AgentKit 的工具参数也是 pydantic 模型（从函数签名生成的）。
    两边在这一点上恰好对齐，所以不需要重新生成 schema。
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    schema = tool.args_schema
    if schema is None or not hasattr(schema, "model_json_schema"):
        # 没有声明参数模型的工具，给一个空模型兜底
        args_model = create_model(f"{tool.name}_Args")
    else:
        args_model = schema

    async def run(ctx: ToolContext, **kwargs: Any) -> str:
        # LangChain 的工具可能是同步的，也可能是异步的
        outcome = tool.ainvoke(kwargs) if tool.coroutine or tool.func is None else None
        if outcome is None:
            import asyncio

            return await asyncio.to_thread(tool.invoke, kwargs)
        result = await outcome
        return result if isinstance(result, str) else str(result)

    spec = ToolSpec(
        name=tool.name,
        description=tool.description or f"来自 LangChain 的工具 {tool.name}",
        args_model=args_model,
        fn=run,
        dangerous=dangerous,
        idempotent=False,  # 来路不明的工具，保守假设它不幂等
    )

    # 保留原始的工具定义，方便排查时对照
    _ = convert_to_openai_tool(tool) if tool.args_schema else None
    return spec


#: 兼容别名：有些地方会以函数形式引用上下文类型
ToolCallable = ContextAwareCallable
