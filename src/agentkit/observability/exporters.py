"""Trace 导出。

三种消费者，三种格式：

* **JSONL** —— 一行一个 trace，直接追加。产出的是**事实**，可以被任何工具消费：
  统计、画图、或者日后做成数据集。
* **控制台** —— 人读的缩进树，调试时用。
* **OTel** —— 可选。要不要真的接一套 OTel 后端取决于有没有人看那些面板；
  没有消费者的遥测是纯成本。

**纪律：导出器不做聚合。** 汇总数字（平均步数、成功率）应该由消费 JSONL 的分析
代码算，而不是让导出器顺手统计一下。一旦导出器开始做聚合，它就不再是「事实的记录」
而是「一份有立场的观点」，换个口径就得改代码重跑历史数据。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from .tracer import Trace

__all__ = [
    "TraceExporter",
    "JsonlExporter",
    "ConsoleExporter",
    "MemoryExporter",
    "read_jsonl",
]


class TraceExporter(Protocol):
    """导出器的协议。实现者只负责把 trace 送出去，不负责加工。"""

    def export(self, trace: Trace) -> None: ...


class JsonlExporter:
    """追加写 JSONL 文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, trace: Trace) -> None:
        # ensure_ascii=False：中文不进 \uXXXX 转义，文件能直接读
        line = json.dumps(trace.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class ConsoleExporter:
    """打印成人读的树。"""

    def __init__(self, *, indent: int = 0) -> None:
        self.indent = indent

    def export(self, trace: Trace) -> None:
        print(trace.render(indent=self.indent))


class MemoryExporter:
    """攒在内存里，供测试和单次查询用。"""

    def __init__(self) -> None:
        self.traces: list[Trace] = []

    def export(self, trace: Trace) -> None:
        self.traces.append(trace)

    def __len__(self) -> int:
        return len(self.traces)

    def __iter__(self) -> Iterator[Trace]:
        return iter(self.traces)


def read_jsonl(path: str | Path) -> list[dict]:
    """读回 JSONL。坏行跳过并记数——一个被截断的尾行不该让整份日志读不出来。"""
    target = Path(path)
    if not target.exists():
        return []

    records: list[dict] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return records


def export_all(traces: Iterable[Trace], exporter: TraceExporter) -> int:
    """批量导出，返回条数。"""
    count = 0
    for trace in traces:
        exporter.export(trace)
        count += 1
    return count
