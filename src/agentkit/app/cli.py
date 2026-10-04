"""命令行入口。

CLI 只是事件流的一个消费者——和 SSE 端点、评测运行器消费的是同一条流。
这是「引擎只 yield 事件」这个设计带来的直接好处：换个前端不需要动引擎。
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
from ..runtime.agent import build_agent
from ..tools.builtin import BUILTIN_GROUPS

__all__ = ["main", "run_once"]

#: 工具结果在终端里的预览长度。完整内容在 --json-events 或 trace 里看。
_PREVIEW_CHARS = 400


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentkit",
        description="AgentKit —— 原生 tool calling 的 Agent 框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_run_parser(subparsers)
    _add_tools_parser(subparsers)

    return parser


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="执行一个任务")
    run.add_argument("prompt", nargs="+", help="任务描述")
    run.add_argument(
        "-w", "--workspace", default=None, help="工作区目录（默认: 当前目录）"
    )
    run.add_argument("-m", "--model", default=None, help="覆盖模型 id")
    run.add_argument("-s", "--max-steps", type=int, default=None, help="最大步数")
    run.add_argument(
        "--json-events",
        action="store_true",
        help="把事件流按 JSONL 打到 stdout，便于管道处理",
    )
    run.add_argument(
        "--groups",
        default=None,
        help=f"逗号分隔的工具分组，可选: {', '.join(BUILTIN_GROUPS)}（默认全部）",
    )
    run.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="高危命令不自动放行，改为告知模型需要人工审批",
    )
    run.add_argument("-q", "--quiet", action="store_true", help="只输出最终答复")


def _add_tools_parser(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser("tools", help="列出内置工具及其参数 schema")


# ---------------------------------------------------------------- 渲染


class ConsoleRenderer:
    """把事件流渲染成人读的终端输出。"""

    def __init__(self, *, quiet: bool = False) -> None:
        self.quiet = quiet

    def __call__(self, event: RunEvent) -> None:
        match event.type:
            case "run_started":
                if not self.quiet:
                    print(f"\n{'─' * 64}")
                    print(f"agent={event.agent}  model={event.model}")
                    print(f"task: {event.input}")
                    print(f"{'─' * 64}")

            case "step_started":
                if not self.quiet:
                    print(f"\n[步骤 {event.step}]")

            case "reasoning_delta":
                if not self.quiet:
                    print(f"  · 思考: {_preview(event.text, 200)}")

            case "text_delta":
                if not self.quiet:
                    print(f"  · 输出: {_preview(event.text, 200)}")

            case "tool_call_started":
                args = json.dumps(event.arguments, ensure_ascii=False)
                print(f"  → {event.tool_name}({_preview(args, 160)})")

            case "tool_result":
                mark = "错误" if event.is_error else "结果"
                preview = _preview(event.content, _PREVIEW_CHARS)
                print(f"  ← {mark} ({event.duration_ms}ms): {preview}")

            case "usage_reported":
                if not self.quiet:
                    u = event.cumulative
                    print(
                        f"  · tokens: 入 {u.input_tokens}（缓存 {u.cached_input_tokens}）"
                        f" / 出 {u.output_tokens}"
                    )

            case "run_completed":
                self._render_completion(event)

            case "run_failed":
                print(f"\n✗ 失败 [{event.error_type}]: {event.message}", file=sys.stderr)

            case "run_cancelled":
                print(f"\n⊘ 已取消: {event.reason}", file=sys.stderr)

    @staticmethod
    def _render_completion(event: RunEvent) -> None:
        print(f"\n{'─' * 64}")
        print(event.text)
        print(f"{'─' * 64}")

        stats = [f"{event.steps} 步", f"{event.duration_ms}ms"]
        u = event.usage
        stats.append(f"入 {u.input_tokens} / 出 {u.output_tokens} tokens")
        if u.cached_input_tokens:
            stats.append(f"缓存命中 {u.cached_input_tokens}")
        if event.cost is not None:
            stats.append(f"约 {event.cost:.4f} {event.currency}")
        print("  ".join(stats))


def _preview(text: str, limit: int) -> str:
    """压成单行并截断，避免工具输出把终端刷屏。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------- 命令

def run_once(
    prompt: str,
    *,
    workspace: Path | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    groups: list[str] | None = None,
    auto_approve: bool = True,
    quiet: bool = False,
    json_events: bool = False,
) -> int:
    """执行一个任务，返回进程退出码。"""
    settings = load_settings(
        model=model,
        max_steps=max_steps,
        workspace=workspace,
    )

    agent = build_agent(
        settings,
        workspace=workspace,
        tool_groups=groups,
        auto_approve=auto_approve,
    )

    renderer = ConsoleRenderer(quiet=quiet)
    failed = False

    async def drive() -> None:
        nonlocal failed
        try:
            async for event in agent.stream(prompt):
                if json_events:
                    # exclude_none 会丢掉 type 以外的可选字段，这里全量输出
                    print(event.model_dump_json(), flush=True)
                else:
                    renderer(event)

                if event.type == "run_failed":
                    failed = True
        finally:
            await agent.aclose()

    try:
        asyncio.run(drive())
    except AgentKitError as exc:
        print(f"✗ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130

    return 1 if failed else 0


def list_tools() -> int:
    """打印内置工具及其参数 schema。"""
    from ..tools.builtin import default_registry

    registry = default_registry()
    for spec in registry.all():
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


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.command == "run":
        groups = None
        if args.groups:
            groups = [g.strip() for g in args.groups.split(",") if g.strip()]
            unknown = set(groups) - set(BUILTIN_GROUPS)
            if unknown:
                print(
                    f"✗ 未知的工具分组: {', '.join(sorted(unknown))}。"
                    f"可选: {', '.join(BUILTIN_GROUPS)}",
                    file=sys.stderr,
                )
                return 2

        return run_once(
            " ".join(args.prompt),
            workspace=Path(args.workspace) if args.workspace else None,
            model=args.model,
            max_steps=args.max_steps,
            groups=groups,
            auto_approve=not args.no_auto_approve,
            quiet=args.quiet,
            json_events=args.json_events,
        )

    if args.command == "tools":
        return list_tools()

    return 2  # pragma: no cover - argparse 的 required=True 已经挡住


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
