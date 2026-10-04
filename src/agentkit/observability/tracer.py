"""追踪：把事件流翻译成 span 树。

**这里没有一行埋点代码。** 追踪器是事件流的**消费者**，不是插在引擎里的探针。
这个区别很实际：

* 引擎里不需要 ``span = tracer.start()`` / ``span.end()`` 这种成对调用——它们迟早
  会有一处漏掉返回值或者走异常分支时没关，于是 trace 里出现永远不结束的 span；
* 追踪逻辑可以脱离引擎单测：喂一段构造好的事件序列进去，断言产出的 span 树；
* 关掉追踪就是 `不消费事件`，没有任何性能影响。

这是「引擎只产事件」这个设计最直接的兑现——代价是事件序列必须能完整还原出
时间结构，所以下面那张映射表是契约，不是实现细节。

属性名对齐 **OpenTelemetry GenAI semantic conventions**。注意该规范目前仍是
Development 状态（未 stable），且 ``gen_ai.system`` 已被弃用，改用
``gen_ai.provider.name``。规范版本变了这里要跟着改。

事件 → span 的映射：

=========================== ==================================================
事件                         动作
=========================== ==================================================
``run_started``              开根 span ``invoke_agent <agent>``
``step_started``             开 ``chat <model>`` span
``usage_reported``           给 chat span 记 usage，并关闭它
``tool_call_started``        开 ``execute_tool <name>`` span
``tool_result``              关闭它，带错误状态
``approval_requested``       给当前工具 span 加一个事件
``run_completed``/``failed`` 关闭根 span，写状态与总量
=========================== ==================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.events import RunEvent
from ..core.ids import new_id
from ..core.usage import Usage

__all__ = [
    "Span",
    "SpanKind",
    "SpanStatus",
    "Trace",
    "TraceBuilder",
    "provider_name_for",
]


class SpanKind(StrEnum):
    """span 的类型。取值与 OTel GenAI 的 ``gen_ai.operation.name`` 一致。"""

    AGENT = "invoke_agent"
    CHAT = "chat"
    TOOL = "execute_tool"


class SpanStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


@dataclass
class Span:
    """一个带时间的操作区间。"""

    name: str
    kind: SpanKind
    span_id: str
    parent_id: str | None = None
    trace_id: str = ""
    start_ns: int = 0
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: SpanStatus = SpanStatus.UNSET
    #: 时间点事件（审批就是挂在工具 span 上的事件，而不是子 span）。
    events: list[dict[str, Any]] = field(default_factory=list)
    children: list[Span] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        end = self.end_ns if self.end_ns is not None else time.time_ns()
        return (end - self.start_ns) / 1_000_000

    @property
    def open(self) -> bool:
        return self.end_ns is None

    def close(self, status: SpanStatus | None = None) -> None:
        if self.end_ns is None:
            self.end_ns = time.time_ns()
        if status is not None:
            self.status = status

    def to_dict(self) -> dict[str, Any]:
        """序列化成可 JSON 化的结构。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": str(self.kind),
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "start_ns": self.start_ns,
            "duration_ms": round(self.duration_ms, 3),
            "status": str(self.status),
            "attributes": self.attributes,
        }
        if self.parent_id:
            payload["parent_id"] = self.parent_id
        if self.events:
            payload["events"] = self.events
        if self.children:
            payload["children"] = [child.to_dict() for child in self.children]
        return payload


@dataclass
class Trace:
    """一次 run 的完整 span 树。"""

    trace_id: str
    run_id: str = ""
    root: Span | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "root": self.root.to_dict() if self.root else None,
        }

    def render(self, *, indent: int = 0) -> str:
        """渲染成缩进树，供终端查看。"""
        if self.root is None:
            return "(空 trace)"
        lines: list[str] = []

        def walk(span: Span, depth: int) -> None:
            pad = "  " * depth
            mark = {"OK": "✓", "ERROR": "✗"}.get(str(span.status), "·")
            detail = _summarize(span)
            lines.append(f"{pad}{mark} {span.name}  {span.duration_ms:.0f}ms{detail}")
            for event in span.events:
                lines.append(f"{pad}    · {event.get('name')}: {event.get('detail', '')}")
            for child in span.children:
                walk(child, depth + 1)

        walk(self.root, indent)
        return "\n".join(lines)


def _summarize(span: Span) -> str:
    """挑几个最能说明问题的属性放在行尾。"""
    attributes = span.attributes
    if span.kind is SpanKind.TOOL:
        if attributes.get("error.type"):
            return f"  [{attributes['error.type']}]"
        return ""
    if span.kind is SpanKind.CHAT:
        usage = (
            f"入 {attributes.get('gen_ai.usage.input_tokens', 0):,}"
            f" / 出 {attributes.get('gen_ai.usage.output_tokens', 0):,}"
        )
        return f"  [{usage}]"
    if span.kind is SpanKind.AGENT and attributes.get("gen_ai.usage.input_tokens"):
        return (
            f"  [共 {attributes.get('gen_ai.usage.input_tokens', 0):,}"
            f" / {attributes.get('gen_ai.usage.output_tokens', 0):,} tokens]"
        )
    return ""


#: 从端点或模型名推断 provider。OTel 的 ``gen_ai.provider.name`` 有约定的取值。
_PROVIDER_HINTS = {
    "deepseek": "deepseek",
    "openai": "openai",
    "anthropic": "anthropic",
    "dashscope": "qwen",
    "moonshot": "kimi",
    "bigmodel": "glm",
}


def provider_name_for(*, base_url: str = "", model: str = "") -> str:
    """推断 provider 名。

    推断不出来时返回 ``"unknown"`` 而不是编一个——``gen_ai.provider.name`` 的取值
    是有约定的枚举，填错比填 ``unknown`` 更糟。
    """
    haystack = f"{base_url} {model}".lower()
    for hint, name in _PROVIDER_HINTS.items():
        if hint in haystack:
            return name
    return "unknown"


class TraceBuilder:
    """消费事件流，构建 span 树。

    用法::

        builder = TraceBuilder(provider="deepseek", model="deepseek-flash")
        async for event in agent.stream(...):
            builder.consume(event)
        trace = builder.finish()
    """

    def __init__(
        self,
        *,
        provider: str = "unknown",
        model: str = "",
        session_id: str | None = None,
        capture_content: bool = False,
        trace_id: str | None = None,
    ) -> None:
        """
        :param capture_content: 是否把工具参数/结果、模型输出记进 span 属性。
            默认关闭——OTel 的 GenAI 规范也把内容捕获列为 opt-in，因为它可能很大，
            也可能含有不该进日志的数据。
        """
        self.provider = provider
        self.model = model
        self.session_id = session_id
        self.capture_content = capture_content

        self.trace = Trace(trace_id=trace_id or new_id("trace"))
        self._root: Span | None = None
        self._chat: Span | None = None
        self._tool: Span | None = None
        self._stack: list[Span] = []

    # ------------------------------------------------------------ 主入口

    def consume(self, event: RunEvent) -> None:
        """处理一个事件。未知类型静默忽略——事件类型会增加，追踪器不该因此崩。"""
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    def finish(self) -> Trace:
        """收尾：关掉所有还开着的 span。

        正常路径下这些已经关了；这里兜底是为了防止一条异常的事件序列在 trace 里
        留下永不结束的 span——那种 trace 看起来像是程序卡住了。
        """
        now = time.time_ns()
        for span in (self._tool, self._chat, self._root):
            if span is not None and span.open:
                span.end_ns = now
                if span.status is SpanStatus.UNSET:
                    span.status = SpanStatus.UNSET
        return self.trace

    # ------------------------------------------------------------ 事件处理

    def _on_run_started(self, event: RunEvent) -> None:
        self.trace.run_id = event.run_id
        root = self._open(
            name=f"invoke_agent {event.agent}",
            kind=SpanKind.AGENT,
            parent=None,
            attributes={
                "gen_ai.operation.name": str(SpanKind.AGENT),
                "gen_ai.provider.name": self.provider,
                "gen_ai.agent.name": event.agent,
                "gen_ai.request.model": event.model or self.model,
            },
        )
        if self.session_id:
            root.attributes["gen_ai.conversation.id"] = self.session_id
        self._root = root
        self.trace.root = root

    def _on_step_started(self, event: RunEvent) -> None:
        # 一次「步骤」在语义上就是一次模型调用：它从发起请求开始，
        # 到 usage 事件（流结束时才发）为止。
        self._chat = self._open(
            name=f"chat {self.model or 'model'}",
            kind=SpanKind.CHAT,
            parent=self._root,
            attributes={
                "gen_ai.operation.name": str(SpanKind.CHAT),
                "gen_ai.provider.name": self.provider,
                "gen_ai.request.model": self.model,
                "gen_ai.agent.name": self._root.attributes.get("gen_ai.agent.name", "")
                if self._root
                else "",
            },
        )

    def _on_usage_reported(self, event: RunEvent) -> None:
        if self._chat is None:
            return
        self._chat.attributes["gen_ai.usage.input_tokens"] = event.usage.input_tokens
        self._chat.attributes["gen_ai.usage.output_tokens"] = event.usage.output_tokens
        if event.usage.cached_input_tokens:
            self._chat.attributes["gen_ai.usage.cached_input_tokens"] = (
                event.usage.cached_input_tokens
            )
        if event.usage.reasoning_tokens:
            self._chat.attributes["gen_ai.usage.reasoning_tokens"] = (
                event.usage.reasoning_tokens
            )
        self._chat.close(SpanStatus.OK)
        self._chat = None

    def _on_text_delta(self, event: RunEvent) -> None:
        if self.capture_content and self._chat is not None:
            self._chat.attributes["gen_ai.output.messages"] = (
                self._chat.attributes.get("gen_ai.output.messages", "") + event.text
            )

    def _on_reasoning_delta(self, event: RunEvent) -> None:
        if self.capture_content and self._chat is not None:
            self._chat.attributes["agentkit.reasoning"] = (
                self._chat.attributes.get("agentkit.reasoning", "") + event.text
            )

    def _on_tool_call_started(self, event: RunEvent) -> None:
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": str(SpanKind.TOOL),
            "gen_ai.tool.name": event.tool_name,
            "gen_ai.tool.type": "function",
            "gen_ai.tool.call.id": event.tool_use_id,
        }
        if self.capture_content:
            attributes["gen_ai.tool.call.arguments"] = event.arguments

        self._tool = self._open(
            name=f"execute_tool {event.tool_name}",
            kind=SpanKind.TOOL,
            parent=self._root,
            attributes=attributes,
        )

    def _on_tool_result(self, event: RunEvent) -> None:
        if self._tool is None:
            return
        if self.capture_content:
            self._tool.attributes["gen_ai.tool.call.result"] = event.content
        if event.is_error:
            # 工具内部失败不等于整个 agent 失败——错误记在工具 span 上，
            # 根 span 只在 agent 真的中止时才失败。这是规范明确要求的。
            self._tool.attributes["error.type"] = _error_type_of(event.content)
            self._tool.close(SpanStatus.ERROR)
        else:
            self._tool.close(SpanStatus.OK)
        self._tool = None

    def _on_approval_requested(self, event: RunEvent) -> None:
        target = self._tool or self._root
        if target is not None:
            target.events.append(
                {
                    "name": "approval_requested",
                    "detail": event.reason,
                    "tool_name": event.tool_name,
                }
            )

    def _on_approval_resolved(self, event: RunEvent) -> None:
        target = self._tool or self._root
        if target is not None:
            target.events.append(
                {
                    "name": "approval_resolved",
                    "detail": event.note,
                    "approved": event.approved,
                }
            )

    def _on_run_completed(self, event: RunEvent) -> None:
        root = self._root
        if root is None:
            return
        root.attributes["gen_ai.usage.input_tokens"] = event.usage.input_tokens
        root.attributes["gen_ai.usage.output_tokens"] = event.usage.output_tokens
        root.attributes["agentkit.steps"] = event.steps
        root.attributes["agentkit.duration_ms"] = event.duration_ms
        if event.cost is not None:
            root.attributes["agentkit.cost"] = event.cost
            root.attributes["agentkit.cost.currency"] = event.currency
        if self.capture_content:
            root.attributes["gen_ai.output.messages"] = event.text
        root.close(SpanStatus.OK)

    def _on_run_failed(self, event: RunEvent) -> None:
        if self._root is None:
            return
        self._root.attributes["error.type"] = event.error_type
        self._root.attributes["agentkit.steps"] = event.steps
        if event.message:
            self._root.events.append({"name": "error", "detail": event.message[:500]})
        self._root.close(SpanStatus.ERROR)

    def _on_run_cancelled(self, event: RunEvent) -> None:
        if self._root is None:
            return
        # 取消不是错误——它是调用方主动做的决定。记成一个事件而不是 ERROR 状态，
        # 否则监控面板上正常的用户取消会显示成故障。
        self._root.events.append(
            {"name": "cancelled", "detail": event.reason, "steps": event.steps}
        )
        self._root.close(SpanStatus.UNSET)

    # ------------------------------------------------------------ 辅助

    def _open(
        self,
        *,
        name: str,
        kind: SpanKind,
        parent: Span | None,
        attributes: dict[str, Any],
    ) -> Span:
        span = Span(
            name=name,
            kind=kind,
            span_id=new_id("span"),
            parent_id=parent.span_id if parent else None,
            trace_id=self.trace.trace_id,
            start_ns=time.time_ns(),
            attributes=attributes,
        )
        if parent is not None:
            parent.children.append(span)
        return span


def _error_type_of(content: str) -> str:
    """从错误结果里提取异常类名。

    工具的错误结果形如 ``"ToolValidationError: ..."``，取冒号前那一段。
    提取不出来就用 ``"ToolError"``，不编造。
    """
    head = content.split(":", 1)[0].strip()
    if head and head.isidentifier():
        return head
    return "ToolError"


def usage_of_trace(trace: Trace) -> Usage:  # pragma: no cover - 便捷函数
    """从 trace 根 span 上取回总用量。"""
    if trace.root is None:
        return Usage()
    return Usage(
        input_tokens=int(trace.root.attributes.get("gen_ai.usage.input_tokens", 0)),
        output_tokens=int(trace.root.attributes.get("gen_ai.usage.output_tokens", 0)),
    )
