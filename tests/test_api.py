"""HTTP 服务：SSE、取消、审批、trace、发现。

整条链路用假模型跑，所以是离线的、确定的。真正要验证的是**协议行为**：
SSE 帧的格式对不对、seq 能不能续传、审批的两段式握手能不能走通。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import pytest
from fastapi.testclient import TestClient

from agentkit.app.api import create_app
from agentkit.core.config import Settings
from agentkit.runtime.agent import Agent
from agentkit.tools.base import ToolSpec
from agentkit.tools.registry import ToolRegistry

from .conftest import ScriptedModel, assistant_text, assistant_tools


def _agent(workspace: Path, *script) -> Agent:
    model = ScriptedModel(*script)
    registry = ToolRegistry()

    async def read_file(ctx, file_path: Annotated[str, "路径"]) -> str:
        """读取文件。"""
        return f"内容:{file_path}"

    registry.add(ToolSpec.from_function(read_file))
    return Agent(
        model=model,
        tools=registry,
        workspace=workspace,
        name="test-agent",
        temperature=None,
    )


@pytest.fixture
def client(workspace: Path, settings: Settings) -> TestClient:
    agent = _agent(workspace, assistant_text("回答完了"))
    app = create_app(agent, settings=settings, base_url="http://testserver")
    with TestClient(app) as test_client:
        yield test_client


def _events(client: TestClient, run_id: str, **params) -> list[dict]:
    """把 SSE 流读干净，返回事件列表。"""
    events: list[dict] = []
    with client.stream("GET", f"/runs/{run_id}/events", params=params) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ---------------------------------------------------------------- 运行


class TestRunLifecycle:
    def test_start_run_returns_id_and_url(self, client: TestClient):
        response = client.post("/runs", json={"prompt": "你好"})
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"].startswith("run_")
        assert body["events_url"].endswith("/events")

    def test_events_flow_to_completion(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        events = _events(client, run_id)

        types = [e["type"] for e in events]
        assert types[0] == "run_started"
        assert types[-1] == "run_completed"
        assert events[-1]["text"] == "回答完了"

    def test_unknown_run_is_404(self, client: TestClient):
        assert client.get("/runs/run_nope").status_code == 404
        assert client.get("/runs/run_nope/events").status_code == 404

    def test_run_snapshot(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        _events(client, run_id)

        body = client.get(f"/runs/{run_id}").json()
        assert body["finished"] is True
        assert body["result"]["type"] == "run_completed"

    def test_empty_prompt_is_rejected(self, client: TestClient):
        assert client.post("/runs", json={"prompt": ""}).status_code == 422


class TestSSEFormat:
    def test_frames_carry_id_and_event_name(self, client: TestClient):
        """``id:`` 行是浏览器 EventSource 自动续传的依据，不能省。"""
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]

        with client.stream("GET", f"/runs/{run_id}/events") as response:
            text = b"".join(response.iter_bytes()).decode()

        assert "id: 1\n" in text
        assert "event: run_started\n" in text
        assert "data: {" in text
        assert text.rstrip().endswith("}") or "\n\n" in text

    def test_seq_is_monotonic(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        events = _events(client, run_id)
        assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


class TestReplay:
    def test_from_seq_replays_only_later_events(self, client: TestClient):
        """断线重连靠它续传——客户端带上收到的最后一个序号，断线期间的事件补齐。"""
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        all_events = _events(client, run_id)
        assert len(all_events) > 2

        cutoff = all_events[1]["seq"]
        replayed = _events(client, run_id, from_seq=cutoff)

        assert replayed
        assert all(e["seq"] > cutoff for e in replayed)
        assert [e["type"] for e in replayed] == [
            e["type"] for e in all_events if e["seq"] > cutoff
        ]


# ---------------------------------------------------------------- 取消


class TestCancellation:
    def test_cancel_running_run(self, workspace: Path, settings: Settings):
        import asyncio

        registry = ToolRegistry()

        async def slow(ctx, x: str = "") -> str:
            """慢工具。"""
            await asyncio.sleep(30)
            return "不该到这"

        registry.add(ToolSpec.from_function(slow))
        agent = Agent(
            model=ScriptedModel(assistant_tools(("slow", {}))),
            tools=registry,
            workspace=workspace,
            temperature=None,
        )
        app = create_app(agent, settings=settings)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "跑"}).json()["run_id"]
            assert client.delete(f"/runs/{run_id}").json()["cancelled"] is True

            events = _events(client, run_id)
            assert events[-1]["type"] == "run_cancelled"

    def test_cancel_finished_run_is_a_no_op(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        _events(client, run_id)

        body = client.delete(f"/runs/{run_id}").json()
        assert body["cancelled"] is False


# ---------------------------------------------------------------- 审批


def _gated_agent(workspace: Path) -> Agent:
    registry = ToolRegistry()

    async def gated(ctx, x: Annotated[str, "参数"] = "") -> str:
        """需要审批的动作。

        拒绝时抛 ``ApprovalDenied``（和真实的 ``run_command`` 一致），
        而不是返回一句普通的话——那样注册表就不会把它标成错误结果，
        模型也就看不出这一步失败了。
        """
        from agentkit.core.errors import ApprovalDenied

        if not await ctx.request_approval(_request(ctx)):
            raise ApprovalDenied("未经批准")
        return "批准了"

    registry.add(ToolSpec.from_function(gated, dangerous=True))
    return Agent(
        model=ScriptedModel(assistant_tools(("gated", {})), assistant_text("完成")),
        tools=registry,
        workspace=workspace,
        temperature=None,
    )


def _wait_for_pending(client: TestClient, run_id: str, deadline: float = 10.0) -> str:
    """轮询直到出现待审批项，返回它的 tool_use_id。

    刻意**不**在 SSE 流的迭代过程中发第二个请求：``TestClient`` 只有一个 portal，
    流还阻塞在 ``await queue.get()`` 上时，嵌套的请求发不出去，测试会挂死。
    真实部署（uvicorn 多连接并发）没有这个问题，这是测试客户端的限制。

    先轮询、再读流，等价于「客户端在另一个线程里点了批准」——和真实行为一致。
    """
    import time

    start = time.monotonic()
    while time.monotonic() - start < deadline:
        pending = client.get(f"/runs/{run_id}/approvals").json()["pending"]
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError(f"等了 {deadline} 秒也没等到审批请求")


class TestApproval:
    def test_approval_round_trip(self, workspace: Path, settings: Settings):
        """两段式握手：服务端挂起等决定，客户端 POST 决定，工具才继续。"""
        agent = _gated_agent(workspace)
        app = create_app(agent, settings=settings)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "跑"}).json()["run_id"]

            tool_use_id = _wait_for_pending(client, run_id)

            decided = client.post(
                f"/runs/{run_id}/approvals/{tool_use_id}",
                json={"approved": True, "note": "测试批准"},
            )
            assert decided.status_code == 200

            events = _events(client, run_id)
            types = [e["type"] for e in events]

            assert "approval_requested" in types
            assert "approval_resolved" in types
            # 请求必须排在决定之前
            assert types.index("approval_requested") < types.index("approval_resolved")

            # 批准之后工具真的跑了
            result = next(e for e in events if e["type"] == "tool_result")
            assert result["content"] == "批准了"
            assert events[-1]["type"] == "run_completed"

    def test_denying_makes_the_tool_fail(self, workspace: Path, settings: Settings):
        agent = _gated_agent(workspace)
        app = create_app(agent, settings=settings)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "跑"}).json()["run_id"]
            tool_use_id = _wait_for_pending(client, run_id)

            client.post(
                f"/runs/{run_id}/approvals/{tool_use_id}",
                json={"approved": False, "note": "不行"},
            )

            events = _events(client, run_id)

        resolved = next(e for e in events if e["type"] == "approval_resolved")
        assert resolved["approved"] is False

        # 拒绝要变成一条**错误**结果回灌给模型，否则它看不出这一步失败了
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["is_error"] is True
        assert "ApprovalDenied" in result["content"]

        # 但整个 run 不该因此失败——模型可以换个做法或者交回给人
        assert events[-1]["type"] == "run_completed"

    def test_approval_request_arrives_while_the_tool_is_still_blocked(
        self, workspace: Path, settings: Settings
    ):
        """**审批请求必须在工具仍然阻塞时就可见。**

        这是事件总线存在的全部理由——如果事件只能在 await 返回之后才产出，
        审批就成了「先等出结果再问你要不要批准」，逻辑是反的。
        这里通过「工具还没结束，但待审批项已经查得到」来证明这一点。
        """
        agent = _gated_agent(workspace)
        app = create_app(agent, settings=settings)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "跑"}).json()["run_id"]
            tool_use_id = _wait_for_pending(client, run_id)

            # 此刻 run 还没结束，工具还挂在那里等
            snapshot = client.get(f"/runs/{run_id}").json()
            assert snapshot["finished"] is False
            assert tool_use_id in snapshot["pending_approvals"]

            client.post(
                f"/runs/{run_id}/approvals/{tool_use_id}", json={"approved": True}
            )
            assert client.get(f"/runs/{run_id}").json()["finished"] is True

    def test_approval_for_unknown_id_is_404(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        _events(client, run_id)

        response = client.post(
            f"/runs/{run_id}/approvals/nope", json={"approved": True}
        )
        assert response.status_code == 404


def _request(ctx):
    from agentkit.tools.policy import ApprovalRequest

    return ApprovalRequest(
        tool_name="gated",
        arguments={},
        reason="测试",
        tool_use_id=ctx.tool_use_id,
    )


# ---------------------------------------------------------------- 追踪


class TestTraceEndpoint:
    def test_trace_is_available_after_completion(self, client: TestClient):
        run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
        _events(client, run_id)

        body = client.get(f"/runs/{run_id}/trace").json()
        assert body["ready"] is True
        root = body["trace"]["root"]
        assert root["name"] == "invoke_agent test-agent"
        assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"

    def test_trace_records_tool_spans(self, workspace: Path, settings: Settings):
        agent = _agent(
            workspace,
            assistant_tools(("read_file", {"file_path": "a.txt"})),
            assistant_text("读完了"),
        )
        app = create_app(agent, settings=settings)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "读"}).json()["run_id"]
            _events(client, run_id)
            root = client.get(f"/runs/{run_id}/trace").json()["trace"]["root"]

        names = [c["name"] for c in root["children"]]
        assert "execute_tool read_file" in names
        assert names.count("execute_tool read_file") == 1

    def test_traces_are_written_to_disk(
        self, workspace: Path, settings: Settings, tmp_path: Path
    ):
        from agentkit.observability import read_jsonl

        agent = _agent(workspace, assistant_text("好"))
        app = create_app(agent, settings=settings, trace_dir=tmp_path)

        with TestClient(app) as client:
            run_id = client.post("/runs", json={"prompt": "你好"}).json()["run_id"]
            _events(client, run_id)

        records = read_jsonl(tmp_path / "traces.jsonl")
        assert len(records) == 1
        assert records[0]["root"]["name"] == "invoke_agent test-agent"


# ---------------------------------------------------------------- 发现


class TestDiscoveryEndpoints:
    def test_well_known_card(self, client: TestClient):
        """A2A 约定的发现端点——任何客户端都能问这个 URL 拿到服务能力。"""
        response = client.get("/.well-known/agent-card.json")
        assert response.status_code == 200

        card = response.json()
        assert card["name"] == "test-agent"
        assert card["protocolVersion"] == "1.0"
        assert card["preferredTransport"] == "JSONRPC"
        assert isinstance(card["skills"], list)

    def test_card_skills_derive_from_registered_tools(self, client: TestClient):
        """技能从实际注册的工具推导，不是手写的一份清单——手写的迟早会对不上。"""
        card = client.get("/.well-known/agent-card.json").json()
        tags = {tag for skill in card["skills"] for tag in skill["tags"]}
        assert "read" in tags  # 注册了 read_file
        assert "shell" not in tags  # 没注册 run_command

    def test_card_declares_real_capabilities(self, client: TestClient):
        """声明了 streaming 却做不到，客户端会在跑到一半时失败。"""
        card = client.get("/.well-known/agent-card.json").json()
        assert card["capabilities"]["streaming"] is True

    def test_list_agents(self, client: TestClient):
        body = client.get("/agents").json()
        assert body["count"] >= 1
        assert any(a["name"] == "test-agent" for a in body["agents"])

    def test_filter_agents_by_tag(self, client: TestClient):
        assert client.get("/agents", params={"tag": "read"}).json()["count"] >= 1
        assert client.get("/agents", params={"tag": "没有这个"}).json()["count"] == 0

    def test_get_unknown_agent_is_404(self, client: TestClient):
        assert client.get("/agents/nope").status_code == 404

    def test_healthz(self, client: TestClient):
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert "active_runs" in body


class TestConsole:
    def test_console_is_served(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "AgentKit" in response.text

    def test_console_does_not_inject_model_output(self):
        """控制台里模型输出一律走 textContent。

        工具结果可能包含任意文件内容，拼 HTML 就是一个 XSS。

        检查的是**危险用法**（赋值、插入）而不是标识符本身——后者会被
        「本文件不使用 innerHTML」这句注释绊倒，而剥注释又剥不干净
        （``//`` 在 URL 里也会出现）。
        """
        import re

        html = (
            Path(__file__).resolve().parent.parent
            / "src" / "agentkit" / "app" / "web" / "index.html"
        ).read_text(encoding="utf-8")

        for pattern in (
            r"\.innerHTML\s*\+?=",
            r"\.outerHTML\s*\+?=",
            r"\.insertAdjacentHTML\s*\(",
            r"document\.write\s*\(",
        ):
            assert not re.search(pattern, html), f"控制台里出现了危险的 HTML 注入写法: {pattern}"

        # 确认确实使用 textContent 渲染，否则上述检查可能仅因未实现任何渲染而通过
        assert html.count("textContent") > 10
