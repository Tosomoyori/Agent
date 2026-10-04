"""命令执行工具。

这是整个工具集里风险最高的一环，所以它的安全模型值得说清楚：

``shell=True`` 天然可注入——这是「执行 shell 命令」这个功能的本质，不是实现缺陷。
真正的缓解手段是**执行前的裁决**（见 :mod:`.policy`）：明确破坏性的拒绝、已知安全的
放行、其余一律转人工审批。默认不信任。

相比之下旧实现的做法——用正则黑名单过滤整条命令串——有两个问题：黑名单必然漏
（``rm -r -f``、``rm${IFS}-rf``、``python -c "..."``），而且会误杀（``echo "rm -rf /"``
只是打印一段字符串）。

Phase 2 会把这里的审批占位换成真正的异步审批事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Annotated

from ...core.errors import ApprovalDenied, CommandDenied
from .._util import truncate
from ..base import ToolContext
from ..policy import ApprovalRequest, Verdict, assess_command
from ..registry import ToolRegistry

__all__ = ["register_shell_tools"]

DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
#: 命令输出的截断上限。编译日志动辄几万行，全塞回上下文纯属浪费。
MAX_OUTPUT_CHARS = 10_000


async def run_command(
    ctx: ToolContext,
    command: Annotated[str, "要执行的 shell 命令。会先经过安全裁决，危险命令会被拒绝"],
    timeout: Annotated[float, "超时秒数，超时会终止进程"] = DEFAULT_TIMEOUT,
) -> str:
    """在工作区中执行一条 shell 命令并返回输出。危险命令会被拒绝或要求人工审批。"""
    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT))

    assessment = assess_command(command)
    if assessment.verdict is Verdict.DENY:
        # 抛错而不是返回字符串：让注册表统一标成 is_error，模型才知道这是一次失败
        # 而不是一条正常结果。
        raise CommandDenied(f"{assessment.reason}\n  命令: {command}")

    if assessment.verdict is Verdict.REQUIRE_APPROVAL:
        decision = await ctx.request_approval(
            ApprovalRequest(
                tool_name="run_command",
                arguments={"command": command},
                reason=assessment.reason,
                command=command,
            )
        )
        if not decision:
            raise ApprovalDenied(
                f"命令未经批准，未执行。{assessment.reason}\n"
                f"  命令: {command}\n"
                "请改用允许清单内的命令，或把人需要做的这一步写进最终答复。"
            )

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ctx.workspace),
            env=_safe_env(),
        )
    except OSError as exc:
        return f"命令启动失败: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        await _terminate(process)
        return f"命令超时（{timeout:g} 秒）已终止: {command}"

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()

    if process.returncode == 0:
        body = out or "(无输出)"
        return f"执行成功（exit=0）\n{truncate(body, limit=MAX_OUTPUT_CHARS)}"

    parts = [f"执行失败（exit={process.returncode}）"]
    if err:
        parts.append(f"--- stderr ---\n{err}")
    if out:
        parts.append(f"--- stdout ---\n{out}")
    return truncate("\n".join(parts), limit=MAX_OUTPUT_CHARS)


def _safe_env() -> dict[str, str]:
    """给子进程的环境变量做一次裁剪。

    暂时只剥离最明显的凭据类变量，避免子进程或它打印的日志泄漏 API Key。
    真正的隔离要靠容器，这里只是纵深防御的一层。
    """
    sensitive_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in sensitive_markers)
    }
    # 保证子进程的编码行为可预期，否则 Windows 上中文输出会变乱码
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """终止一个超时的进程。"""
    if process.returncode is not None:
        return
    # 进程可能刚好在 kill 之前自行退出，这时两个调用都会抛异常，直接忽略
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), timeout=5)


def register_shell_tools(registry: ToolRegistry) -> None:
    """把命令执行工具注册进给定的注册表。"""
    registry.register(
        run_command,
        dangerous=True,
        # 同一条命令重复执行可能有副作用（比如 `git commit`），
        # 所以重放恢复时不能拿它当幂等的。
        idempotent=False,
    )


#: 当前平台的 shell 名称，写进系统提示词用。
SHELL_NAME = "cmd.exe" if sys.platform == "win32" else "/bin/sh"
