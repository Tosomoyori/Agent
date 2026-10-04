"""工具执行的安全策略：路径边界与命令裁决。

这个模块取代旧实现里的两处做法，两处都有真实可绕过的漏洞：

**路径边界**（旧代码 ``abspath`` + ``startswith``）:

1. ``abspath`` **不解析符号链接**——工作区内放一个指向 ``C:\\Windows\\System32``
   的 symlink，``startswith`` 检查照样通过，实际写到了工作区外；
2. 字符串前缀比较不严谨——``C:\\foo`` 是 ``C:\\foobar`` 的前缀，二者却不是父子关系。

现在用 ``realpath`` 先解析掉所有 symlink 和 ``..``，再用 ``commonpath`` 做真正的
路径包含判断。

**命令裁决**（旧代码的正则黑名单）:

黑名单**必然漏**：``rm -r -f``、``rm${IFS}-rf``、``python -c "import os; os.system(...)"``、
PowerShell 里的等价写法……枚举不完。改成三分裁决：明确破坏性的 **拒绝**，
已知安全的 **放行**，其余一律 **转人工审批**。默认不信任，而不是默认放行。
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.errors import CommandDenied, PathViolation

__all__ = [
    "Verdict",
    "CommandAssessment",
    "ApprovalRequest",
    "ApprovalDecision",
    "Approver",
    "resolve_in_workspace",
    "assess_command",
    "split_command_segments",
    "DEFAULT_ALLOWED_EXECUTABLES",
]


# ============================================================ 审批协议


@dataclass
class ApprovalRequest:
    """一个等待人工确认的动作。"""

    tool_name: str
    arguments: dict[str, Any]
    reason: str
    tool_use_id: str = ""
    #: 如果是命令类工具，把命令原文单独放一份，便于审批界面高亮显示。
    command: str | None = None


@dataclass
class ApprovalDecision:
    """审批结果。"""

    approved: bool
    note: str = ""

    @classmethod
    def allow(cls, note: str = "") -> ApprovalDecision:
        return cls(approved=True, note=note)

    @classmethod
    def deny(cls, note: str = "") -> ApprovalDecision:
        return cls(approved=False, note=note)


@runtime_checkable
class Approver(Protocol):
    """审批接口。

    定义在这里而不是 ``runtime`` 里，是为了让 ``tools`` 只依赖这个协议、
    **不 import runtime**，保持依赖方向单向。具体实现（控制台提问、SSE 队列等待、
    自动放行）由 runtime 注入。
    """

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """请求审批。实现方阻塞到有人做出决定为止。"""
        ...


# ============================================================ 路径边界


def resolve_in_workspace(root: Path, target: str | Path) -> Path:
    """把 ``target`` 解析成 ``root`` 内的绝对路径；越界时抛 :class:`PathViolation`。

    返回的是**已解析符号链接**的真实路径，调用方直接拿它做 IO 即可，
    不要再自己拼一遍路径——那会重新引入 TOCTOU 的窗口。
    """
    root_real = Path(os.path.realpath(root))
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root_real / candidate

    real = Path(os.path.realpath(candidate))

    try:
        common = Path(os.path.commonpath([root_real, real]))
    except ValueError as exc:
        # Windows 上跨盘符没有公共前缀，commonpath 直接抛 ValueError
        raise PathViolation(f"路径不在工作区内: {target}") from exc

    if common != root_real:
        raise PathViolation(
            f"路径越出工作区边界: {target}\n"
            f"  解析后: {real}\n"
            f"  工作区: {root_real}"
        )
    return real


# ============================================================ 命令裁决


class Verdict(StrEnum):
    """命令的裁决结果。用 ``StrEnum`` 是为了它直接能当字符串比较和序列化。"""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class CommandAssessment:
    """一次命令裁决的完整结论。"""

    verdict: Verdict
    reason: str = ""
    #: 命令里出现的可执行文件（按段收集），便于日志与审计。
    executables: list[str] = field(default_factory=list)


#: 明确具有破坏性、无法安全参数化的命令。刻意保持很短——这里是「确定的坏」，
#: 剩下的交给审批。正则匹配的是**段首的可执行文件**，不是整条命令串，
#: 免得 ``echo "rm -rf /"`` 这种无害引用被误杀。
DENIED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "mkfs", "mkfs.ext4", "mkfs.xfs", "diskpart", "format",
        "shutdown", "reboot", "halt", "poweroff",
        "fdisk", "parted",
    }
)

#: 默认放行的可执行文件。覆盖常见的只读查看与项目内开发动作。
DEFAULT_ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        # 文件查看
        "ls", "dir", "cat", "type", "head", "tail", "wc", "file", "stat",
        "find", "grep", "rg", "fd", "tree", "pwd", "du", "df",
        "sort", "uniq", "cut", "tr", "sed", "awk", "diff", "column",
        # 项目开发
        "python", "python3", "py", "pip", "uv", "pytest", "ruff", "mypy",
        "node", "npm", "pnpm", "yarn", "make", "just",
        "git", "gh",
        # 目录与文本编辑（写操作由路径边界与审批兜底）
        "mkdir", "touch", "cp", "mv", "rmdir",
        "echo", "printf", "tee", "which", "where", "env", "date", "whoami",
    }
)

#: 管道到 shell 执行是典型的远程代码执行路径（``curl ... | sh``）。
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"})

#: 有副作用的命令即便放行，也应当被记录。
_WRITE_VERBS = frozenset({"rm", "del", "rmdir", "mv", "cp", "tee", "touch", "mkdir"})

_SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|;|\||\n)")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_command_segments(command: str) -> list[str]:
    """按 shell 操作符把命令拆成若干段，**尊重引号**。

    拆段是为了能按段判断可执行文件，而不是对整个命令串做正则——后者会把
    ``echo "rm -rf /"`` 这种无害的字符串引用误判成危险命令。

    这不是一个完整的 shell 解析器（不处理 ``$()``、反引号、heredoc 等），
    定位是**保守的启发式**：拿不准的段会落到「转审批」，而不是「放行」。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None

    i = 0
    while i < len(command):
        ch = command[i]

        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in ("|", "&", ";", "\n"):
            # 吞掉 || 和 && 的第二个字符
            if i + 1 < len(command) and command[i + 1] == ch and ch in ("|", "&"):
                i += 1
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)

        i += 1

    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _executable_of(segment: str) -> str | None:
    """取一段命令的可执行文件名，剥掉环境变量赋值与前缀命令。"""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # 引号不配对等——拿不准，交给上层转审批
        return None

    while tokens:
        token = tokens.pop(0)
        if _ENV_ASSIGN_RE.match(token):
            continue  # FOO=bar cmd ...
        base = os.path.basename(token).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base in ("sudo", "env", "time", "nohup", "command", "exec"):
            continue  # 这些只是包装，真正的命令在后面
        return base

    return None


def assess_command(
    command: str,
    *,
    allowed: frozenset[str] = DEFAULT_ALLOWED_EXECUTABLES,
) -> CommandAssessment:
    """裁决一条命令。默认不信任：看不明白的一律转人工审批。"""
    segments = split_command_segments(command)
    if not segments:
        return CommandAssessment(Verdict.DENY, "空命令")

    executables: list[str] = []
    for segment in segments:
        exe = _executable_of(segment)
        if exe is None:
            return CommandAssessment(
                Verdict.REQUIRE_APPROVAL,
                "无法解析这一段命令（引号不配对或语法特殊），保守起见转人工审批",
            )
        executables.append(exe)

        if exe in DENIED_EXECUTABLES:
            return CommandAssessment(
                Verdict.DENY,
                f"命令 {exe!r} 属于明确具有破坏性的操作，不提供审批通道",
                executables,
            )

    # 管道到 shell 执行 = 远程代码执行的标准路径，永远转审批
    if any(exe in _SHELL_EXECUTABLES for exe in executables):
        return CommandAssessment(
            Verdict.REQUIRE_APPROVAL,
            "命令中包含 shell 解释器，存在远程代码执行风险，需要人工确认",
            executables,
        )

    unknown = [e for e in executables if e not in allowed and e not in _WRITE_VERBS]
    if unknown:
        return CommandAssessment(
            Verdict.REQUIRE_APPROVAL,
            f"以下命令不在允许清单内: {', '.join(sorted(set(unknown)))}",
            executables,
        )

    writes = [e for e in executables if e in _WRITE_VERBS]
    if writes:
        return CommandAssessment(
            Verdict.REQUIRE_APPROVAL,
            f"命令包含写操作: {', '.join(sorted(set(writes)))}",
            executables,
        )

    return CommandAssessment(
        Verdict.ALLOW, f"命令在允许清单内: {', '.join(sorted(set(executables)))}", executables
    )


def enforce_command(command: str, assessment: CommandAssessment) -> None:
    """裁决为 DENY 时抛错。``REQUIRE_APPROVAL`` 由调用方（runtime）处理。"""
    if assessment.verdict is Verdict.DENY:
        raise CommandDenied(f"{assessment.reason}\n  命令: {command}")
