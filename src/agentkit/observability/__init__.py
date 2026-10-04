"""可观测性：把事件流变成 trace。

.. code-block:: python

    observer = Observer(provider="deepseek", model="deepseek-flash")
    async for event in observer.wrap(agent.stream("问题")):
        print(event.type)
    print(observer.trace.render())
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from ..core.events import RunEvent
from .exporters import (
    ConsoleExporter,
    JsonlExporter,
    MemoryExporter,
    TraceExporter,
    export_all,
    read_jsonl,
)
from .tracer import Span, SpanKind, SpanStatus, Trace, TraceBuilder, provider_name_for

__all__ = [
    "ConsoleExporter",
    "JsonlExporter",
    "MemoryExporter",
    "Observer",
    "Span",
    "SpanKind",
    "SpanStatus",
    "Trace",
    "TraceBuilder",
    "TraceExporter",
    "export_all",
    "provider_name_for",
    "read_jsonl",
]


class Observer:
    """把「跑一次」和「记录一次 trace」绑在一起。

    用法是「包住事件流」，而不是往引擎里插探针——引擎完全不知道追踪器的存在。
    """

    def __init__(
        self,
        *,
        provider: str = "unknown",
        model: str = "",
        session_id: str | None = None,
        capture_content: bool = False,
        exporters: Iterable[TraceExporter] = (),
        trace_id: str | None = None,
    ) -> None:
        self.builder = TraceBuilder(
            provider=provider,
            model=model,
            session_id=session_id,
            capture_content=capture_content,
            trace_id=trace_id,
        )
        self.exporters = list(exporters)
        self.trace: Trace | None = None

    async def wrap(self, stream: AsyncIterator[RunEvent]) -> AsyncIterator[RunEvent]:
        """转接事件流，顺带构建 trace。

        **调用方如果中途 ``break``，必须显式关闭**，否则收尾与导出会推迟到垃圾
        回收时（异步生成器的 ``finally`` 不是同步执行的）。正确写法::

            async with contextlib.aclosing(observer.wrap(stream)) as events:
                async for event in events:
                    ...

        之所以做成生成器而不是「消费到底再返回」，是因为服务端要在转发的同时
        逐条推给 SSE 客户端——攒到最后一次性导出，流式就没有意义了。
        """
        try:
            async for event in stream:
                self.builder.consume(event)
                yield event
        finally:
            self.trace = self.builder.finish()
            for exporter in self.exporters:
                exporter.export(self.trace)
