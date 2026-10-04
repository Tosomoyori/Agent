"""文件系统工具。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from .._util import human_size, numbered_lines, truncate
from ..base import ToolContext
from ..registry import ToolRegistry

__all__ = ["register_fs_tools"]

#: 超过这个大小就不读了。二进制和超大文件对模型没有价值，只会挤占上下文。
MAX_READ_BYTES = 2 * 1024 * 1024


async def read_file(
    ctx: ToolContext,
    file_path: Annotated[str, "文件路径，相对于工作区。例如 README.md 或 src/app.py"],
    with_line_numbers: Annotated[bool, "是否给每行加行号，便于引用具体位置。默认 true"] = True,
) -> str:
    """读取文本文件的内容。大文件会截断，只保留开头和结尾。"""
    path = ctx.resolve(file_path)  # 越界会抛 PathViolation，由 registry 转成错误结果

    if not path.exists():
        return f"文件不存在: {ctx.display_path(path)}"
    if path.is_dir():
        return f"{ctx.display_path(path)} 是一个目录，请用 list_directory。"

    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        return (
            f"文件过大（{human_size(size)}，上限 {human_size(MAX_READ_BYTES)}），拒绝读取。"
            "请改用 search_in_file 定位需要的部分。"
        )

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{ctx.display_path(path)} 不是 UTF-8 文本（可能是二进制文件），无法读取。"
    except OSError as exc:
        return f"读取失败: {exc}"

    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    header = f"{ctx.display_path(path)}（{lines} 行，{human_size(size)}）"
    body = numbered_lines(text) if with_line_numbers else text
    return f"{header}\n{truncate(body)}"


async def write_file(
    ctx: ToolContext,
    file_path: Annotated[str, "文件路径，相对于工作区。父目录不存在时会自动创建"],
    content: Annotated[str, "要写入的完整内容。会覆盖文件原有内容"],
) -> str:
    """把内容写入文件（覆盖已有内容），父目录不存在时自动创建。"""
    path = ctx.resolve(file_path)

    if path.exists() and path.is_dir():
        return f"{ctx.display_path(path)} 是一个目录，无法写入。"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 保留调用方给的换行符，不跟随平台做转换
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        return f"写入失败: {exc}"

    lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"已写入 {ctx.display_path(path)}（{lines} 行，{len(content)} 字符）"


async def list_directory(
    ctx: ToolContext,
    path: Annotated[str, "目录路径，相对于工作区。默认当前工作区根目录"] = ".",
    recursive: Annotated[bool, "是否递归列出子目录。大目录慎用，默认 false"] = False,
    max_entries: Annotated[int, "最多列出多少个条目"] = 200,
) -> str:
    """列出目录下的文件和子目录，带大小。"""
    directory = ctx.resolve(path)

    if not directory.exists():
        return f"目录不存在: {ctx.display_path(directory)}"
    if not directory.is_file() and not directory.is_dir():
        return f"{ctx.display_path(directory)} 既不是文件也不是目录。"

    lines: list[str] = []
    truncated = False

    if recursive:
        for root, dirnames, filenames in os.walk(directory):
            # 跳过噪音目录，否则一个 .venv 就能刷屏
            dirnames[:] = sorted(
                d for d in dirnames if d not in {".git", "__pycache__", ".venv", "node_modules"}
            )
            for name in sorted(filenames):
                if len(lines) >= max_entries:
                    truncated = True
                    break
                full = Path(root) / name
                rel = ctx.display_path(full)
                lines.append(f"  {rel}  {human_size(full.stat().st_size)}")
            if truncated:
                break
    else:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
        for entry in entries[:max_entries]:
            if entry.is_dir():
                lines.append(f"  [目录] {entry.name}/")
            else:
                try:
                    size = human_size(entry.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(f"  {entry.name}  {size}")
        truncated = len(entries) > max_entries

    if not lines:
        return f"{ctx.display_path(directory)} 是空目录。"

    header = f"{ctx.display_path(directory)}（{len(lines)} 项）"
    if truncated:
        header += "（已截断）"
    return header + "\n" + "\n".join(lines)


def register_fs_tools(registry: ToolRegistry) -> None:
    """把文件系统工具注册进给定的注册表。

    描述统一取自各函数的 docstring 首行，不在这里重复一遍——两处维护迟早会对不上，
    而对不上的 schema 描述会直接误导模型。
    """
    registry.register(read_file)
    # write_file 有副作用，标记出来供 Phase 2 的审批与幂等键使用
    registry.register(write_file, dangerous=True, idempotent=True)
    registry.register(list_directory)
