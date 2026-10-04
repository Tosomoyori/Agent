"""运行时事件——CLI / SSE / 可观测 / 评测共享的唯一真相源。

引擎**只 yield 事件**，不返回字符串。最终答复是最后一个 :class:`RunCompleted`
事件里的字段，而不是另一条返回值路径。这样做的理由是有四个消费者要看同一条流：

* CLI 要逐步打印进度；
* SSE 端点要把事件转发给浏览器；
* 可观测层要在事件边界上开关 span、累加用量；
* 评测要按轨迹算「工具选对没有」「花了几步」这类指标。

如果引擎改成「返回字符串 + 注册回调」，就出现了两个真相源：回调里能看到的
token 级信息，返回值里看不到，两者还可能不一致。事件流没有这个问题。

一条正常 run 的事件序列大致是::

    RunStarted
    StepStarted(1)
      UsageReported(1)
      ToolCallStarted(read_file) → ToolResult(read_file)
      ToolCallStarted(list_directory) → ToolResult(list_directory)
    StepStarted(2)
      UsageReported(2)
    RunCompleted

失败或取消时以 :class:`RunFailed` / :class:`RunCancelled` 收尾，两者互斥且必居其一。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from .usage import Usage


class _Event(BaseModel):
    """所有事件的基类。"""

    #: 单调递增的序号，从 1 开始。SSE 断线重连时靠它去重和续传。
    seq: int = 0
    run_id: str = ""


# ---------------------------------------------------------------- 生命周期


class RunStarted(_Event):
    type: Literal["run_started"] = "run_started"
    agent: str = ""
    model: str = ""
    input: str = ""


class StepStarted(_Event):
    type: Literal["step_started"] = "step_started"
    step: int = 0


class RunCompleted(_Event):
    type: Literal["run_completed"] = "run_completed"
    text: str = ""
    steps: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost: float | None = None
    currency: str = ""
    duration_ms: int = 0


class RunFailed(_Event):
    type: Literal["run_failed"] = "run_failed"
    error_type: str = ""
    message: str = ""
    steps: int = 0


class RunCancelled(_Event):
    type: Literal["run_cancelled"] = "run_cancelled"
    reason: str = "用户取消"
    steps: int = 0


# ---------------------------------------------------------------- 模型输出


class ReasoningDelta(_Event):
    """思维链增量。DeepSeek 的 ``reasoning_content`` 走这里。"""

    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str = ""


class TextDelta(_Event):
    """正文增量。非流式模式下整段文本一次性发出。"""

    type: Literal["text_delta"] = "text_delta"
    text: str = ""


class UsageReported(_Event):
    type: Literal["usage_reported"] = "usage_reported"
    step: int = 0
    usage: Usage = Field(default_factory=Usage)
    #: 本次 run 到此为止的累计用量。
    cumulative: Usage = Field(default_factory=Usage)


# ---------------------------------------------------------------- 工具


class ToolCallStarted(_Event):
    type: Literal["tool_call_started"] = "tool_call_started"
    tool_use_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_Event):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = ""
    tool_name: str = ""
    content: str = ""
    is_error: bool = False
    duration_ms: int = 0


# ---------------------------------------------------------------- 审批


class ApprovalRequested(_Event):
    """工具正要执行一个需要人工确认的动作。

    建模成事件而不是 ``input()`` 阻塞读，是因为服务化之后根本没有 stdin 可读——
    审批必须能异步地「发出去、等回来」。
    """

    type: Literal["approval_requested"] = "approval_requested"
    tool_use_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ApprovalResolved(_Event):
    type: Literal["approval_resolved"] = "approval_resolved"
    tool_use_id: str = ""
    approved: bool = False
    note: str = ""


#: 所有运行事件的判别联合。``type`` 字段是判别器，反序列化时据此选出具体类型。
RunEvent = Annotated[
    RunStarted
    | StepStarted
    | RunCompleted
    | RunFailed
    | RunCancelled
    | ReasoningDelta
    | TextDelta
    | UsageReported
    | ToolCallStarted
    | ToolResult
    | ApprovalRequested
    | ApprovalResolved,
    Field(discriminator="type"),
]

#: 终止事件类型——出现任意一个就表示这条流不会再产生事件了。
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled"})
