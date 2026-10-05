"""命令行入口。

CLI 只是事件流的一个消费者——和 SSE 端点、评测运行器消费的是同一条流。
换个前端不需要动引擎，这是「引擎只产出事件」这个设计的直接好处。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ..core.config import load_settings
from ..core.errors import AgentKitError
from ..core.events import RunEvent
from ..llm.catalog import capabilities_for
from ..memory.manager import MemoryManager
from ..runtime.agent import Agent, build_agent
from ..runtime.approval import ConsoleApprover, DenyApprover
from ..runtime.budget import Budget
from ..tools.builtin import BUILTIN_GROUPS

__all__ = ["main", "run_once"]

#: 工具结果在终端里的预览长度。完整内容在 --json-events 或 trace 里看。
_PREVIEW_CHARS = 400

DEFAULT_DB = ".agentkit/sessions.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentkit",
        description="AgentKit —— 原生 tool calling 的 Agent 框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_run_parser(subparsers)
    _add_chat_parser(subparsers)
    _add_serve_parser(subparsers)
    _add_eval_parser(subparsers)
    subparsers.add_parser("tools", help="列出内置工具及其参数 schema")
    _add_sessions_parser(subparsers)

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-w", "--workspace", default=None, help="工作区目录（默认: 当前目录）"
    )
    parser.add_argument("-m", "--model", default=None, help="覆盖模型 id")
    parser.add_argument("-s", "--max-steps", type=int, default=None, help="最大步数")
    parser.add_argument(
        "--groups",
        default=None,
        help=f"逗号分隔的工具分组，可选: {', '.join(BUILTIN_GROUPS)}（默认全部）",
    )
    parser.add_argument(
        "--no-approve",
        action="store_true",
        help="关闭审批通道。需要审批的动作会被直接拒绝（比自动放行安全）",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="本次 run 的成本上限（单位与模型定价一致）",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="本次 run 的输入+输出 token 上限",
    )
    parser.add_argument("--db", default=None, help=f"会话数据库路径（默认 {DEFAULT_DB}）")
    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="把模型的思维链也打出来（默认只计数，避免刷屏）",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终答复")


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="执行一个任务")
    run.add_argument("prompt", nargs="+", help="任务描述")
    run.add_argument("--session", default=None, help="会话 id。给了就在这个会话里续跑")
    run.add_argument(
        "--json-events",
        action="store_true",
        help="把事件流按 JSONL 打到 stdout，便于管道处理",
    )
    _add_common_options(run)


def _add_chat_parser(subparsers: argparse._SubParsersAction) -> None:
    chat = subparsers.add_parser("chat", help="进入交互式对话（同一会话内保持记忆）")
    chat.add_argument("--session", default="default", help="会话 id（默认 default）")
    _add_common_options(chat)


def _add_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    serve = subparsers.add_parser("serve", help="启动 HTTP 服务与 Web 控制台")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")
    serve.add_argument(
        "--reload", action="store_true", help="改动代码后自动重启（开发用）"
    )
    _add_common_options(serve)
    # 服务端不用终端审批——没有 stdin 可读。审批走 QueueApprover，
    # 由客户端通过 POST /runs/{id}/approvals/{tool_use_id} 决定。
    serve.set_defaults(no_approve=True)


def _add_sessions_parser(subparsers: argparse._SubParsersAction) -> None:
    sessions = subparsers.add_parser("sessions", help="管理会话")
    sessions.add_argument(
        "action", choices=["list", "show", "delete"], nargs="?", default="list"
    )
    sessions.add_argument("session_id", nargs="?", default=None, help="会话 id")
    sessions.add_argument("--db", default=None, help=f"会话数据库路径（默认 {DEFAULT_DB}）")


# ---------------------------------------------------------------- 渲染


class ConsoleRenderer:
    """把事件流渲染成人读的终端输出。

    正文增量**直接接着上一个印**，不换行——否则一句话会被拆成几十行。
    思维链默认只计数：实测里它可能占输出 token 的绝大多数（回答"数到五"
    会产生上千个思维链 token），逐条打印会把真正要看的内容冲走。
    """

    def __init__(self, *, quiet: bool = False, show_reasoning: bool = False) -> None:
        self.quiet = quiet
        self.show_reasoning = show_reasoning
        self._reset()

    def _reset(self) -> None:
        self._reasoning_chars = 0
        #: 光标是否停在一行中间（刚打印过增量但没换行）。
        self._line_open = False
        #: 停着的那行是正文还是思维链。类型不同才需要换行——
        #: 同一类型的连续增量必须接着印，否则一句话会被拆成十几行。
        self._line_kind = ""
        #: 本次 run 是否已经把正文流式印出来了。收尾时据此决定要不要重印一遍答案。
        #: 这个标记**不能**在 _flush_line 里清掉——清了就会把答案印两遍，
        #: 因为 usage 事件夹在正文和收尾事件之间。
        self._text_written = False

    def __call__(self, event: RunEvent) -> None:
        match event.type:
            case "run_started":
                self._reset()
                if not self.quiet:
                    print(f"\nagent={event.agent}  model={event.model}")
                    print(f"task: {event.input}")
                    print("─" * 64)

            case "step_started":
                if not self.quiet:
                    print(f"\n[{event.step}]", end=" ")

            case "reasoning_delta":
                self._reasoning_chars += len(event.text)
                if self.show_reasoning:
                    if self._line_open and self._line_kind != "reasoning":
                        self._flush_line()
                    if not self._line_open:
                        print("\n  💭 ", end="")
                        self._line_open = True
                        self._line_kind = "reasoning"
                    print(event.text, end="", flush=True)

            case "text_delta":
                # 只有从思维链切回正文时才需要换行；正文的连续片段必须接着印
                if self._line_open and self._line_kind != "text":
                    self._flush_line()
                print(event.text, end="", flush=True)
                self._line_open = True
                self._line_kind = "text"
                self._text_written = True

            case "tool_call_started":
                self._flush_line()
                args = json.dumps(event.arguments, ensure_ascii=False)
                print(f"  → {event.tool_name}({_preview(args, 160)})")

            case "tool_result":
                mark = "错误" if event.is_error else "结果"
                preview = _preview(event.content, _PREVIEW_CHARS)
                print(f"  ← {mark} ({event.duration_ms}ms): {preview}")

            case "approval_requested":
                self._flush_line()
                print(f"  ⏸ 需要审批: {event.tool_name} — {event.reason}")

            case "approval_resolved":
                mark = "✓" if event.approved else "✗"
                verdict = "已批准" if event.approved else "已拒绝"
                note = f"（{event.note}）" if event.note else ""
                print(f"  {mark} {verdict}{note}")

            case "usage_reported":
                if not self.quiet:
                    self._render_usage(event)

            case "run_completed":
                self._render_completion(event)

            case "run_failed":
                self._flush_line()
                print(f"\n✗ 失败 [{event.error_type}]: {event.message}", file=sys.stderr)

            case "run_cancelled":
                self._flush_line()
                print(f"\n⊘ 已取消: {event.reason}", file=sys.stderr)

    def _flush_line(self) -> None:
        """把停在半行的光标收掉。幂等。"""
        if self._line_open:
            print()
            self._line_open = False
            self._line_kind = ""

    def _render_usage(self, event: RunEvent) -> None:
        self._flush_line()
        u = event.cumulative
        parts = [f"入 {u.input_tokens:,}", f"出 {u.output_tokens:,}"]
        if u.cached_input_tokens:
            parts.append(f"缓存 {u.cached_input_tokens:,}")
        if self._reasoning_chars:
            parts.append(f"思维链 {self._reasoning_chars:,} 字")
        print(f"  · {'  '.join(parts)}")

    def _render_completion(self, event: RunEvent) -> None:
        if self._text_written:
            self._flush_line()  # 正文已经边生成边印了，不再重复一遍
        else:
            print(f"\n{event.text}")

        stats = [f"{event.steps} 步", f"{event.duration_ms}ms"]
        u = event.usage
        stats.append(f"入 {u.input_tokens:,} / 出 {u.output_tokens:,} tokens")
        if u.cached_input_tokens:
            stats.append(f"缓存命中 {u.cached_input_tokens:,}")
        if event.cost is not None:
            stats.append(f"约 {event.cost:.4f} {event.currency}")
        print("─" * 64)
        print("  ".join(stats))


def _preview(text: str, limit: int) -> str:
    """压成单行并截断，避免工具输出把终端刷屏。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------- 组装


def _force_utf8_stdio() -> None:
    """在 Windows 上把标准输出切到 UTF-8。

    没有这一步，输出重定向到管道时 Python 会用系统的 ANSI 代码页（简中环境下是
    cp936），中文会变成乱码。只影响输出编码，不改文件读写的行为。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # 极少数终端不支持切换编码，静默忽略即可——不影响功能
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def _resolve_groups(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    groups = [g.strip() for g in raw.split(",") if g.strip()]
    unknown = set(groups) - set(BUILTIN_GROUPS)
    if unknown:
        raise SystemExit(
            f"✗ 未知的工具分组: {', '.join(sorted(unknown))}。"
            f"可选: {', '.join(BUILTIN_GROUPS)}"
        )
    return groups


def _build(
    args: argparse.Namespace,
    *,
    db_path: Path | None,
    use_memory: bool,
) -> tuple[Agent, MemoryManager | None]:
    """按命令行参数组装 agent 与（可选的）记忆管理器。"""
    workspace = Path(args.workspace) if getattr(args, "workspace", None) else None
    settings = load_settings(
        model=getattr(args, "model", None),
        max_steps=getattr(args, "max_steps", None),
        workspace=workspace,
    )

    memory = None
    if use_memory and db_path is not None:
        # 用模型声明的真实上下文窗口，而不是写死一个数——裁多了浪费，
        # 裁少了直接撑爆请求。未知模型会落到能力目录里的保守默认值。
        memory = MemoryManager.open(
            db_path,
            max_context_tokens=capabilities_for(settings.model).context_window,
        )

    user_groups = _resolve_groups(getattr(args, "groups", None))
    auto = not getattr(args, "no_approve", False)
    # --no-approve 时用 DenyApprover 而不是 None：语义是「别问我，但也别自作主张」。
    # 传 None 和传 DenyApprover 在这里行为相同，但显式一点更好读日志。
    approver = ConsoleApprover() if auto else DenyApprover("已用 --no-approve 关闭审批")

    agent = build_agent(
        settings,
        workspace=workspace,
        tool_groups=user_groups,
        approver=approver,
        budget=Budget(
            max_cost=getattr(args, "max_cost", None),
            max_tokens=getattr(args, "max_tokens", None),
        ),
        memory=memory,
    )
    return agent, memory


# ---------------------------------------------------------------- 命令


def run_once(args: argparse.Namespace, *, db_path: Path | None) -> int:
    """执行一个任务，返回进程退出码。"""
    session_id = getattr(args, "session", None)
    agent, memory = _build(args, db_path=db_path, use_memory=bool(session_id))

    renderer = ConsoleRenderer(
        quiet=args.quiet, show_reasoning=args.show_reasoning
    )
    failed = False

    async def drive() -> None:
        nonlocal failed
        try:
            async for event in agent.stream(" ".join(args.prompt), session_id=session_id):
                if args.json_events:
                    print(event.model_dump_json(), flush=True)
                else:
                    renderer(event)
                if event.type == "run_failed":
                    failed = True
        finally:
            await agent.aclose()
            if memory is not None:
                memory.close()

    try:
        asyncio.run(drive())
    except AgentKitError as exc:
        print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130

    return 1 if failed else 0


def chat(args: argparse.Namespace, *, db_path: Path | None) -> int:
    """交互式对话。整个会话共用一个 session_id，所以上下文能延续。"""
    agent, memory = _build(args, db_path=db_path, use_memory=True)
    renderer = ConsoleRenderer(
        quiet=args.quiet, show_reasoning=args.show_reasoning
    )
    session_id = args.session

    print(f"会话: {session_id}    工作区: {agent.workspace}")
    print("输入问题开始对话，'exit' 退出，'/new' 换一个会话\n")

    async def ask(prompt: str) -> int:
        failed = False
        async for event in agent.stream(prompt, session_id=session_id):
            renderer(event)
            if event.type == "run_failed":
                failed = True
        return 1 if failed else 0

    try:
        while True:
            try:
                prompt = input("\n你: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                return 0

            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit", "退出"):
                print("再见！")
                return 0
            if prompt == "/new":
                import uuid

                session_id = f"session_{uuid.uuid4().hex[:8]}"
                print(f"已切换到新会话: {session_id}")
                continue

            asyncio.run(ask(prompt))
    finally:
        asyncio.run(agent.aclose())
        if memory is not None:
            memory.close()


def serve(args: argparse.Namespace, *, db_path: Path | None) -> int:
    """启动 HTTP 服务与 Web 控制台。

    服务端**不用**终端审批：没有 stdin 可读。需要审批的动作会发出
    ``ApprovalRequested`` 事件，由客户端 POST 决定回来。默认策略是拒绝，
    所以没人接手审批时任务会失败而不是偷偷放行。
    """
    import uvicorn

    from .api import create_app

    workspace = Path(args.workspace) if args.workspace else None
    settings = load_settings(
        model=args.model, max_steps=args.max_steps, workspace=workspace
    )

    memory = MemoryManager.open(
        db_path or Path(DEFAULT_DB),
        max_context_tokens=capabilities_for(settings.model).context_window,
    )

    agent = build_agent(
        settings,
        workspace=workspace,
        tool_groups=_resolve_groups(args.groups),
        approver=DenyApprover("服务端未接入审批通道，请通过 API 提交决定"),
        budget=Budget(max_cost=args.max_cost, max_tokens=args.max_tokens),
        memory=memory,
    )

    trace_dir = (db_path.parent if db_path else Path(".agentkit"))
    app = create_app(
        agent,
        settings=settings,
        trace_dir=trace_dir,
        base_url=f"http://{args.host}:{args.port}",
    )

    print(f"控制台  http://{args.host}:{args.port}/")
    print(f"Agent 卡片  http://{args.host}:{args.port}/.well-known/agent-card.json")
    print(f"模型  {settings.model}    工作区  {agent.workspace}")
    print(f"trace  {trace_dir / 'traces.jsonl'}")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        memory.close()
    return 0


def list_sessions(db_path: Path) -> int:
    if not db_path.exists():
        print(f"还没有会话记录（{db_path} 不存在）")
        return 0

    memory = MemoryManager.open(db_path)
    try:
        sessions = asyncio.run(memory.list_sessions())
    finally:
        memory.close()

    if not sessions:
        print("还没有会话记录")
        return 0

    import datetime

    print(f"{'会话':<28} {'消息数':>6}  最后更新")
    for info in sessions:
        stamp = datetime.datetime.fromtimestamp(info.updated_at).strftime("%Y-%m-%d %H:%M")
        print(f"{info.id:<28} {info.message_count:>6}  {stamp}")
    return 0


def show_session(db_path: Path, session_id: str) -> int:
    memory = MemoryManager.open(db_path)
    try:
        messages = asyncio.run(memory.history(session_id))
    finally:
        memory.close()

    if not messages:
        print(f"会话 {session_id} 没有消息")
        return 1

    for index, message in enumerate(messages, 1):
        label = {"user": "用户", "assistant": "助手", "system": "系统"}[message.role]
        body = message.text() or (
            f"[{len(message.tool_uses())} 次工具调用]"
            if message.tool_uses()
            else f"[{len(message.tool_results())} 条工具结果]"
        )
        print(f"\n{index:>3} {label}: {body[:300]}")
    return 0


def delete_session(db_path: Path, session_id: str) -> int:
    memory = MemoryManager.open(db_path)
    try:
        ok = asyncio.run(memory.delete_session(session_id))
    finally:
        memory.close()
    print(f"{'已删除' if ok else '没找到'} 会话 {session_id}")
    return 0 if ok else 1


def list_tools() -> int:
    """打印内置工具及其参数 schema。"""
    from ..tools.builtin import default_registry

    for spec in default_registry().all():
        flags = []
        if spec.dangerous:
            flags.append("有副作用")
        if not spec.idempotent:
            flags.append("非幂等")
        suffix = f"  [{'、'.join(flags)}]" if flags else ""
        print(f"\n{spec.name}{suffix}")
        print(f"  {spec.description}")
        print("  参数:")
        print(json.dumps(spec.json_schema(), ensure_ascii=False, indent=4))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.command == "tools":
        return list_tools()

    if args.command == "run":
        db_path = Path(args.db) if args.db else Path(DEFAULT_DB)
        return run_once(args, db_path=db_path)

    if args.command == "chat":
        db_path = Path(args.db) if args.db else Path(DEFAULT_DB)
        return chat(args, db_path=db_path)

    if args.command == "eval":
        return run_eval(args)

    if args.command == "serve":
        db_path = Path(args.db) if args.db else Path(DEFAULT_DB)
        return serve(args, db_path=db_path)

    if args.command == "sessions":
        db_path = Path(args.db) if args.db else Path(DEFAULT_DB)
        if args.action == "list":
            return list_sessions(db_path)
        if not args.session_id:
            print("需要给出 session_id", file=sys.stderr)
            return 2
        if args.action == "show":
            return show_session(db_path, args.session_id)
        return delete_session(db_path, args.session_id)

    return 2  # pragma: no cover - argparse 的 required=True 已经挡住


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# ---------------------------------------------------------------- 评测


def _add_eval_parser(subparsers: argparse._SubParsersAction) -> None:
    ev = subparsers.add_parser("eval", help="跑评测套件并出报告")
    ev.add_argument("suite", help="套件路径（.jsonl 文件或目录）")
    ev.add_argument("-n", "--trials", type=int, default=1, help="每个用例重复几次（默认 1）")
    ev.add_argument(
        "--mode",
        choices=["hermetic", "live", "both"],
        default="live",
        help="hermetic 用假模型回放（不花钱，验证评测框架本身）；live 真实调用",
    )
    ev.add_argument("-c", "--concurrency", type=int, default=3, help="并发数（默认 3）")
    ev.add_argument("--out", default="eval_reports", help="报告输出目录")
    ev.add_argument("--tag", action="append", default=None, help="只跑带这个标签的用例")
    ev.add_argument("--case", action="append", default=None, help="只跑指定 id 的用例")
    ev.add_argument("--keep-workspaces", action="store_true", help="保留工作区便于排查")
    _add_common_options(ev)


def run_eval(args: argparse.Namespace) -> int:
    """跑评测。这是唯一会真花钱的命令，所以费用相关信息都摆在明面上。"""
    from ..evaluation import EvalRunner, load_cases, write_report

    cases = load_cases(args.suite)
    if args.tag:
        wanted = set(args.tag)
        cases = [c for c in cases if wanted & set(c.tags)]
    if args.case:
        wanted_ids = set(args.case)
        cases = [c for c in cases if c.id in wanted_ids]

    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 1

    settings = load_settings(
        model=args.model,
        max_steps=args.max_steps,
        workspace=Path(args.workspace) if args.workspace else None,
    )
    workspace_base = Path(args.workspace) if args.workspace else Path.cwd()
    work_root = workspace_base / ".agentkit" / "eval"
    out_dir = Path(args.out)
    suite_name = Path(args.suite).stem

    modes = ["hermetic", "live"] if args.mode == "both" else [args.mode]
    exit_code = 0

    for mode in modes:
        runnable = [c for c in cases if mode == "live" or c.hermetic_ready]
        skipped = len(cases) - len(runnable)
        if not runnable:
            print(f"[{mode}] 没有可跑的用例，跳过", file=sys.stderr)
            continue

        if mode == "live":
            print(
                f"[live] {len(runnable)} 个用例 × {args.trials} 次 = "
                f"{len(runnable) * args.trials} 次真实调用，**会产生 API 费用**",
                file=sys.stderr,
            )
        else:
            print(f"[hermetic] {len(runnable)} 个用例（假模型回放，不消耗 API）")

        runner = EvalRunner(
            _factory_for(mode, settings, workspace_base),
            trials=args.trials,
            max_concurrency=args.concurrency,
            work_root=work_root,
            mode=mode,
            keep_workspaces=args.keep_workspaces,
        )

        result = asyncio.run(runner.run(runnable, suite_name=suite_name))
        md_path, json_path = write_report(result, out_dir)

        print(render_summary(result))
        if skipped:
            print(f"  （跳过 {skipped} 个用例：没有 hermetic 脚本）")
        print(f"\n报告: {md_path}    {json_path}")

        if result.pass_pow_k() < 1.0 and mode == "hermetic":
            # hermetic 跑不满分，说明要么脚本写错了，要么判据写错了——
            # 两种都该修，不该带着一个红的基线继续
            print("  ⚠️ hermetic 模式没有全过，先检查用例的脚本与判据", file=sys.stderr)
            exit_code = 1

    return exit_code


def _factory_for(mode: str, settings, workspace_base: Path):
    """给出「工作区 → agent」的工厂。两种模式的差别只在这里。"""
    from ..evaluation.replay import build_from_script
    from ..runtime.agent import build_agent
    from ..tools.builtin import register_builtin_tools

    if mode == "hermetic":

        def hermetic(workspace: Path, case):
            return Agent(
                model=build_from_script(case.script),
                tools=register_builtin_tools(groups=["fs", "search"]),
                workspace=workspace,
                name="eval-hermetic",
                max_steps=settings.max_steps,
                temperature=None,
            )

        return hermetic

    def live(workspace: Path, case):
        return build_agent(
            settings,
            workspace=workspace,
            tool_groups=["fs", "search"],
            approver=DenyApprover("评测环境不接审批通道"),
        )

    return live


def render_summary(result) -> str:
    """终端里的一行结论。完整数据在报告里。"""
    k = result.trials
    return (
        f"\n  TSR {result.task_success_rate():.1%}"
        f"   pass@{k} {result.pass_at_k():.1%}"
        f"   pass^{k} {result.pass_pow_k():.1%}"
        f"   工具准确率 {_pct(result.tool_call_accuracy())}"
        f"   平均 {result.average_steps():.1f} 步"
        f"   耗时 {result.duration_s:.1f}s"
    )


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"
