"""HTTP 服务：SSE 流式、取消、审批、trace、agent 发现。

**为什么 run 是异步的、事件要单独拉。** 一个 run 可能跑几十秒。如果 POST 直接挂着
返回结果，中途就没法取消、没法审批、断线也没法续。所以：POST 立刻返回 run_id，
事件走 SSE，审批和取消是独立的端点——三件事各自独立，任何一个卡住都不影响其余。

**断线续传靠 ``seq``。** 每个事件都带单调递增的序号，服务端保留回放缓冲。
客户端重连时带 ``from_seq=N`` 就能补齐断线期间的事件。这就是
:mod:`agentkit.core.events` 里那个 ``seq`` 字段存在的理由——不是为了好看。

**审批是两段式的。** 引擎的 ``ApprovalRequested`` 事件推给客户端，客户端
POST 决定回来，工具才继续执行。这就是为什么引擎的事件得走队列而不是直接 yield
（见 :mod:`agentkit.runtime.bus`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..core.config import Settings, load_settings
from ..core.events import TERMINAL_EVENT_TYPES, RunEvent
from ..core.ids import new_run_id
from ..discovery.card import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
)
from ..discovery.registry import AgentRegistry
from ..llm.catalog import capabilities_for
from ..observability import JsonlExporter, Observer, provider_name_for
from ..runtime.agent import Agent, build_agent
from ..runtime.approval import QueueApprover
from ..runtime.cancellation import CancellationToken

logger = logging.getLogger(__name__)

__all__ = ["RunManager", "RunState", "create_app", "build_card"]

WEB_DIR = Path(__file__).parent / "web"


class RunRequest(BaseModel):
    """发起一次 run。"""

    prompt: str = Field(min_length=1)
    session_id: str | None = None
    max_steps: int | None = None


class ApprovalBody(BaseModel):
    """对一次审批请求做出决定。"""

    approved: bool
    note: str = ""


# ---------------------------------------------------------------- 运行状态


@dataclass
class RunState:
    """一个活跃（或刚结束）的 run。"""

    run_id: str
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    approver: QueueApprover = field(default_factory=QueueApprover)

    task: asyncio.Task | None = None
    #: 已产出的事件，用于断线重连时回放。
    events: list[RunEvent] = field(default_factory=list)
    #: 正在监听的 SSE 连接。放 ``None`` 表示事件流结束。
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    observer: Observer | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.done.is_set()

    def terminal_event(self) -> RunEvent | None:
        for event in reversed(self.events):
            if event.type in TERMINAL_EVENT_TYPES:
                return event
        return None

    def snapshot(self) -> dict[str, Any]:
        """给非 SSE 客户端的一次性状态查询。"""
        terminal = self.terminal_event()
        return {
            "run_id": self.run_id,
            "finished": self.finished,
            "event_count": len(self.events),
            "pending_approvals": self.approver.pending_ids,
            "result": terminal.model_dump() if terminal else None,
        }


class RunManager:
    """管理活跃的 run：启动、广播事件、取消、回收。"""

    def __init__(self, agent: Agent, *, trace_dir: Path | None = None) -> None:
        self.agent = agent
        self.trace_dir = trace_dir
        self.runs: dict[str, RunState] = {}
        self._reaper: asyncio.Task | None = None

    # ------------------------------------------------------------ 生命周期

    def start_reaper(self) -> None:
        """启动后台回收。没有它，跑一整天的服务会把每个 run 的事件都留在内存里。"""
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None

        for state in list(self.runs.values()):
            state.cancellation.cancel("服务关闭")
            if state.task is not None and not state.task.done():
                state.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await state.task

        await self.agent.aclose()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.reap()

    def reap(self) -> int:
        """回收已结束且没人监听的 run。

        判据是「结束了」+「没有订阅者」，不按时间——事件已经落进 trace 文件，
        需要复盘时从那里读，内存里那份没有保留价值。
        """
        stale = [
            run_id
            for run_id, state in self.runs.items()
            if state.finished and not state.subscribers
        ]
        for run_id in stale:
            self.runs.pop(run_id, None)
        return len(stale)

    # ------------------------------------------------------------ 启动

    def start(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        max_steps: int | None = None,
    ) -> RunState:
        run_id = new_run_id()
        state = RunState(run_id=run_id)
        state.observer = self._make_observer(run_id, session_id)
        state.task = asyncio.create_task(
            self._pump(state, prompt, session_id=session_id, max_steps=max_steps)
        )
        self.runs[run_id] = state
        return state

    def _make_observer(self, run_id: str, session_id: str | None) -> Observer:
        exporters = []
        if self.trace_dir is not None:
            exporters.append(JsonlExporter(self.trace_dir / "traces.jsonl"))
        return Observer(
            provider=provider_name_for(
                model=self.agent.model.name,
            ),
            model=self.agent.model.name,
            session_id=session_id,
            exporters=exporters,
        )

    async def _pump(
        self,
        state: RunState,
        prompt: str,
        *,
        session_id: str | None,
        max_steps: int | None,
    ) -> None:
        """把 agent 的事件流转发到所有订阅者。"""
        overrides: dict[str, Any] = {}
        if max_steps is not None:
            overrides["max_steps"] = max_steps

        try:
            stream = self.agent.stream(
                prompt,
                session_id=session_id,
                cancellation=state.cancellation,
                approver=state.approver,
            )
            wrapped = (
                state.observer.wrap(stream) if state.observer is not None else stream
            )

            # aclosing 不是可选的：循环会在终止事件处 break，而异步生成器的
            # finally 在 break 时不会同步执行——不显式关闭，trace 的导出就要等到
            # 垃圾回收，时机不确定。这里要的是「run 结束 = trace 已落盘」。
            async with contextlib.aclosing(wrapped) as events:
                async for event in events:
                    state.events.append(event)
                    for queue in list(state.subscribers):
                        queue.put_nowait(event)
                    if event.type in TERMINAL_EVENT_TYPES:
                        break
        except asyncio.CancelledError:
            state.error = "服务终止了这个 run"
            raise
        except Exception as exc:  # noqa: BLE001 - 一个 run 失败不该拖垮服务
            logger.exception("run %s 失败", state.run_id)
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.done.set()
            for queue in list(state.subscribers):
                queue.put_nowait(None)

    # ------------------------------------------------------------ 订阅

    async def subscribe(
        self, state: RunState, *, from_seq: int = 0
    ) -> AsyncIterator[RunEvent]:
        """订阅一个 run 的事件，先回放再实时。

        :param from_seq: 只回放序号大于它的。客户端重连时带上自己收到的最后一个
            序号，断线期间的事件就补齐了。
        """
        for event in state.events:
            if event.seq > from_seq:
                yield event

        if state.finished:
            return

        queue: asyncio.Queue = asyncio.Queue()
        state.subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            state.subscribers.discard(queue)


# ---------------------------------------------------------------- 应用


def build_card(agent: Agent, settings: Settings, *, base_url: str = "") -> AgentCard:
    """为这个服务构造一张 AgentCard。"""
    capabilities = capabilities_for(settings.model)
    skills = _skills_for(agent)

    return AgentCard(
        name=agent.name,
        description="基于 AgentKit 的通用任务 agent：能读写文件、搜索、执行命令。",
        url=base_url,
        version="0.1.0",
        protocol_version="1.0",
        preferred_transport="JSONRPC",
        capabilities=AgentCapabilities(
            # 声明要和实际能力一致——声明了 streaming 却做不到，
            # 客户端会在跑到一半时失败
            streaming=capabilities.streaming,
            push_notifications=False,
            state_transition_history=True,
        ),
        provider=AgentProvider(organization="agentkit", url=""),
        skills=skills,
        model=settings.model,
        extensions={
            "agentkit.tools": agent.tools.names(),
            "agentkit.context_window": capabilities.context_window,
            "agentkit.budget": {
                "max_steps": agent.max_steps,
            },
        },
    )


def _skills_for(agent: Agent) -> list[AgentSkill]:
    """按启用的工具分组生成技能。

    技能从**实际注册的工具**推导，而不是手写一份清单——手写的迟早和实现对不上，
    而发现机制正是靠这份数据决定「该不该把任务派给这个 agent」。
    """
    names = set(agent.tools.names())
    skills: list[AgentSkill] = []

    if names & {"read_file", "list_directory", "find_files", "search_in_file"}:
        skills.append(
            AgentSkill(
                id="code-navigation",
                name="代码库检索",
                description="在项目里定位文件、搜索内容、读取源码。",
                tags=["code", "search", "read", "files"],
                examples=["找出所有用到 requests 的地方", "这个模块的入口在哪"],
            )
        )

    if "write_file" in names:
        skills.append(
            AgentSkill(
                id="file-authoring",
                name="文件编辑",
                description="写入或修改工作区内的文件。",
                tags=["write", "files", "edit"],
                examples=["把配置里的超时改成 30 秒"],
            )
        )

    if "run_command" in names:
        skills.append(
            AgentSkill(
                id="command-execution",
                name="命令执行",
                description="在工作区里运行 shell 命令（高危动作需要人工审批）。",
                tags=["shell", "execute", "build", "test"],
                examples=["跑一下测试", "看看 git 状态"],
            )
        )

    return skills


def create_app(
    agent: Agent | None = None,
    *,
    settings: Settings | None = None,
    registry: AgentRegistry | None = None,
    trace_dir: Path | None = None,
    base_url: str = "",
) -> FastAPI:
    """构造 FastAPI 应用。

    :param agent: 注入一个现成的 agent。测试里会传一个接假模型的进来，
        这样整条 HTTP 链路可以离线验证。
    """
    settings = settings or load_settings()
    agent = agent or build_agent(settings)
    registry = registry if registry is not None else AgentRegistry()

    manager = RunManager(agent, trace_dir=trace_dir)

    card = build_card(agent, settings, base_url=base_url)
    # 把自己的卡片也放进注册表——「本进程能提供什么能力」应该是可查询的
    if card.name not in registry:
        registry.register(card)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager.start_reaper()
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(
        title="AgentKit",
        description="从零实现的 Agent 开发框架",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.registry = registry
    app.state.agent = agent

    # ------------------------------------------------------------ 控制台

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def console() -> HTMLResponse:
        index = WEB_DIR / "index.html"
        if not index.exists():  # pragma: no cover - 打包漏文件时的兜底
            return HTMLResponse("<h1>AgentKit</h1><p>控制台文件缺失。</p>", status_code=500)
        return HTMLResponse(index.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 运行

    @app.post("/runs")
    async def start_run(body: RunRequest) -> dict[str, Any]:
        state = manager.start(
            body.prompt, session_id=body.session_id, max_steps=body.max_steps
        )
        return {"run_id": state.run_id, "events_url": f"/runs/{state.run_id}/events"}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return _require_run(manager, run_id).snapshot()

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, from_seq: int = 0):
        """SSE 事件流。重连时带上 ``from_seq`` 即可续传。"""
        state = _require_run(manager, run_id)

        async def generator() -> AsyncIterator[bytes]:
            try:
                async for event in manager.subscribe(state, from_seq=from_seq):
                    if await request.is_disconnected():
                        break
                    yield _format_sse(event)
            finally:
                # 连接断了不代表 run 该停——用户可能只是刷新了页面。
                # 真要停就走 DELETE。
                pass

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # 关掉 nginx 之类的缓冲，否则事件会被攒着一起发，流式就没意义了
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete("/runs/{run_id}")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        state = _require_run(manager, run_id)
        if state.finished:
            return {"run_id": run_id, "cancelled": False, "reason": "run 已结束"}
        state.cancellation.cancel("客户端请求取消")
        return {"run_id": run_id, "cancelled": True}

    @app.get("/runs/{run_id}/trace")
    async def get_trace(run_id: str) -> dict[str, Any]:
        state = _require_run(manager, run_id)
        if state.observer is None or state.observer.trace is None:
            # run 还没结束，trace 树尚未收敛完
            return {"run_id": run_id, "ready": False}
        return {"run_id": run_id, "ready": True, "trace": state.observer.trace.to_dict()}

    # ------------------------------------------------------------ 审批

    @app.post("/runs/{run_id}/approvals/{tool_use_id}")
    async def resolve_approval(
        run_id: str, tool_use_id: str, body: ApprovalBody
    ) -> dict[str, Any]:
        state = _require_run(manager, run_id)
        ok = state.approver.resolve(tool_use_id, body.approved, body.note)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"没有待审批项 {tool_use_id!r}（可能已被处理或 run 已结束）",
            )
        return {"run_id": run_id, "tool_use_id": tool_use_id, "approved": body.approved}

    @app.get("/runs/{run_id}/approvals")
    async def list_approvals(run_id: str) -> dict[str, Any]:
        state = _require_run(manager, run_id)
        return {"run_id": run_id, "pending": state.approver.pending_ids}

    # ------------------------------------------------------------ 发现

    @app.get("/.well-known/agent-card.json")
    async def well_known_card() -> JSONResponse:
        """A2A 约定的发现端点。任何客户端都可以问这个 URL 拿到服务能力。"""
        return JSONResponse(card.to_well_known())

    @app.get("/agents")
    async def list_agents(tag: str | None = None) -> dict[str, Any]:
        cards = registry.discover(tag) if tag else registry.all()
        return {
            "count": len(cards),
            "agents": [c.to_well_known() for c in cards],
        }

    @app.get("/agents/{name}")
    async def get_agent(name: str) -> dict[str, Any]:
        found = registry.get(name)
        if found is None:
            raise HTTPException(status_code=404, detail=f"没有名为 {name!r} 的 agent")
        return found.to_well_known()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "model": settings.model,
            "active_runs": sum(1 for s in manager.runs.values() if not s.finished),
        }

    return app


def _require_run(manager: RunManager, run_id: str) -> RunState:
    state = manager.runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"没有这个 run: {run_id}")
    return state


def _format_sse(event: RunEvent) -> bytes:
    """序列化成 SSE 帧。

    同时写 ``id:`` 行——浏览器原生 ``EventSource`` 重连时会自动带上
    ``Last-Event-ID`` 头，服务端据此续传。我们自己也提供 ``from_seq`` 参数，
    两条路都能走。
    """
    payload = json.dumps(event.model_dump(), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n".encode()
