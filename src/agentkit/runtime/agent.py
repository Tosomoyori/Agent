"""Agent——把模型、工具、引擎组装起来的门面。

.. code-block:: python

    agent = build_agent(load_settings(), workspace=Path.cwd())
    result = await agent.run("统计 src 下有多少个 Python 文件")

或者直接消费事件流（CLI 和 SSE 都是这么用的）::

    async for event in agent.stream("..."):
        print(event.type, event)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import Settings, load_settings
from ..core.events import RunEvent
from ..core.types import Message
from ..llm.base import ChatModel
from ..llm.catalog import capabilities_for
from ..llm.openai_compat import OpenAICompatModel
from ..tools.builtin import register_builtin_tools
from ..tools.registry import ToolRegistry
from .engine import Engine, RunResult, collect

__all__ = ["Agent", "build_agent", "build_model"]


@dataclass
class Agent:
    """一个可运行的 agent。"""

    model: ChatModel
    tools: ToolRegistry
    workspace: Path
    name: str = "agent"
    system_prompt: str | None = None
    max_steps: int = 15
    temperature: float | None = 0.0
    max_tokens: int | None = None
    #: 是否自动批准需要审批的动作。
    #: 默认 ``True`` 是为了让本地 CLI 开箱可用；服务化场景应当显式设为 ``False``，
    #: 让审批真正走人工。Phase 2 会把这里换成异步审批事件。
    auto_approve: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def engine(self) -> Engine:
        return Engine(
            self.model,
            self.tools,
            workspace=self.workspace,
            name=self.name,
            system_prompt=self.system_prompt,
            max_steps=self.max_steps,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            auto_approve=self.auto_approve,
        )

    def stream(
        self,
        user_input: str,
        *,
        history: Sequence[Message] | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        """跑一次，逐个 yield 事件。"""
        return self.engine().run(user_input, history=history, run_id=run_id)

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[Message] | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        """跑一次并汇聚成结果。需要中间过程就用 :meth:`stream`。"""
        return await collect(self.stream(user_input, history=history, run_id=run_id))

    async def aclose(self) -> None:
        await self.model.aclose()


def build_model(settings: Settings) -> ChatModel:
    """按配置构造 LLM 适配器。"""
    return OpenAICompatModel(
        model=settings.model,
        api_key=settings.require_api_key(),
        base_url=settings.base_url,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        capabilities=capabilities_for(settings.model),
    )


def build_agent(
    settings: Settings | None = None,
    *,
    workspace: Path | None = None,
    tools: ToolRegistry | None = None,
    tool_groups: list[str] | None = None,
    **overrides: object,
) -> Agent:
    """按配置组装一个开箱可用的 agent。

    :param tool_groups: 只启用指定的内置工具分组。只读场景可以传 ``["fs", "search"]``
        把 shell 排除掉。
    """
    settings = settings or load_settings()

    resolved_workspace = Path(workspace or settings.resolved_workspace()).resolve()
    if not resolved_workspace.is_dir():
        raise NotADirectoryError(f"工作区不存在或不是目录: {resolved_workspace}")

    if tools is None:
        tools = register_builtin_tools(groups=tool_groups)

    params: dict[str, object] = {
        "model": build_model(settings),
        "tools": tools,
        "workspace": resolved_workspace,
        "max_steps": settings.max_steps,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
    }
    params.update({k: v for k, v in overrides.items() if v is not None})
    return Agent(**params)  # type: ignore[arg-type]
