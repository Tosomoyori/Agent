"""搜索工具。"""

from __future__ import annotations

import re
from typing import Annotated

from ...core.errors import ToolValidationError
from .._util import human_size, truncate
from ..base import ToolContext
from ..registry import ToolRegistry

__all__ = ["register_search_tools"]

#: 递归搜索时跳过的目录。一个 .venv 或 node_modules 能产生几万条无意义命中。
SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache",
     ".pytest_cache", "dist", "build", ".idea", ".tox"}
)

#: 单文件搜索的大小上限——压缩过的资源文件经常是单行几 MB，正则会卡死。
MAX_FILE_BYTES = 5 * 1024 * 1024


async def search_in_file(
    ctx: ToolContext,
    pattern: Annotated[str, "搜索模式。默认按正则解释，用 literal=true 可改为纯文本匹配"],
    file_path: Annotated[str, "要搜索的文件路径，相对于工作区"],
    literal: Annotated[bool, "按纯文本匹配而不是正则。模式含特殊字符时建议开启"] = False,
    context_lines: Annotated[int, "每条命中额外显示的上下文行数"] = 0,
    max_matches: Annotated[int, "最多返回多少条命中"] = 50,
) -> str:
    """在单个文件里搜索内容，返回带行号的命中位置。"""
    path = ctx.resolve(file_path)

    if not path.is_file():
        return f"文件不存在: {ctx.display_path(path)}"

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return (
            f"文件过大（{human_size(size)}），跳过搜索。"
            "请先用 list_directory 确认，或换用 run_command 里的 grep。"
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        return f"无法读取 {ctx.display_path(path)}: {exc}"

    try:
        regex = re.compile(re.escape(pattern) if literal else pattern, re.IGNORECASE)
    except re.error as exc:
        # 抛 ToolValidationError 而不是返回字符串：这是**参数**不合法，
        # 走和 schema 校验失败同一条回灌路径，模型才知道要改的是参数。
        raise ToolValidationError(
            f"正则表达式不合法: {exc}。如果本意是纯文本匹配，请设置 literal=true。"
        ) from exc

    lines = text.splitlines()
    hits: list[str] = []
    total = 0

    for index, line in enumerate(lines):
        if not regex.search(line):
            continue
        total += 1
        if len(hits) >= max_matches:
            continue

        if context_lines > 0:
            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            for ctx_index in range(start, end):
                marker = ">" if ctx_index == index else " "
                hits.append(f"{marker} {ctx_index + 1:>5}| {lines[ctx_index]}")
            if end < len(lines):
                hits.append("  ...")
        else:
            hits.append(f"{index + 1:>5}| {line}")

    if total == 0:
        return f"在 {ctx.display_path(path)} 中没有找到 {pattern!r}。"

    header = f"{ctx.display_path(path)}: {total} 处命中"
    if total > len(hits) and context_lines == 0:
        header += f"（只显示前 {max_matches} 处）"
    return truncate(header + "\n" + "\n".join(hits))


def _safe_glob(root, pattern: str, limit: int):
    """递归 glob，跳过噪音目录。

    ``Path.rglob`` 会把 ``.venv`` 里的几万个文件也遍历一遍，所以这里手写遍历
    以便在目录层就剪枝。
    """
    import fnmatch

    results = []
    stack = [root]
    while stack and len(results) < limit:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(
                str(entry.relative_to(root)), pattern
            ):
                results.append(entry)
                if len(results) >= limit:
                    break
    return sorted(results)


async def find_files(
    ctx: ToolContext,
    pattern: Annotated[str, "文件名或相对路径的匹配模式，例如 *.py 或 src/**/test_*.py"],
    path: Annotated[str, "搜索起点目录，相对于工作区"] = ".",
    max_results: Annotated[int, "最多返回多少个文件"] = 100,
) -> str:
    """按文件名模式在工作区里查找文件，自动跳过 .venv、node_modules 等目录。"""
    root = ctx.resolve(path)
    if not root.is_dir():
        return f"目录不存在: {ctx.display_path(root)}"

    matches = _safe_glob(root, pattern, max_results)
    if not matches:
        return f"没有找到匹配 {pattern!r} 的文件（起点: {ctx.display_path(root)}）。"

    lines = [f"  {ctx.display_path(m)}" for m in matches]
    header = f"匹配 {pattern!r} 的文件: {len(matches)} 个"
    if len(matches) >= max_results:
        header += f"（达到上限 {max_results}，结果可能不完整）"
    return header + "\n" + "\n".join(lines)


def register_search_tools(registry: ToolRegistry) -> None:
    """把搜索工具注册进给定的注册表。"""
    registry.register(search_in_file)
    registry.register(find_files)
