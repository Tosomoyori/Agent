"""内置工具共用的辅助函数。"""

from __future__ import annotations

__all__ = ["truncate", "human_size", "numbered_lines"]

#: 单个工具结果的默认字符上限。
#:
#: 旧实现把 ``read_file`` 的完整内容直接塞回上下文——读一个几 MB 的日志或
#: 压缩过的 JS 文件就能把上下文撑爆，而模型真正需要的往往只是开头和结尾。
#: 这里做头尾保留式截断，并在中间明确标注省略了多少内容，让模型知道自己看到的是片段。
DEFAULT_OUTPUT_LIMIT = 12_000


def truncate(text: str, *, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """超长文本做头尾保留式截断，中间标注省略量。

    保留尾部而不是只留头部，是因为错误信息和总结往往在末尾（比如 traceback、
    ``pytest`` 的汇总行）。
    """
    if len(text) <= limit:
        return text

    head_size = limit * 2 // 3
    tail_size = limit - head_size
    omitted = len(text) - head_size - tail_size

    return (
        f"{text[:head_size]}\n\n"
        f"…… [中间省略 {omitted:,} 个字符，原文共 {len(text):,} 个字符] ……\n\n"
        f"{text[-tail_size:]}"
    )


def human_size(num_bytes: int) -> str:
    """把字节数格式化成人读的形式。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"  # pragma: no cover - 循环必然提前返回


def numbered_lines(text: str, start: int = 1) -> str:
    """给每行加上行号，方便模型引用具体位置。"""
    width = len(str(start + text.count("\n")))
    return "\n".join(
        f"{i:>{width}}| {line}" for i, line in enumerate(text.splitlines(), start)
    )
