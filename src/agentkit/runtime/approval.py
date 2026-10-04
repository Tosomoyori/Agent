"""审批通道的具体实现。

``tools.policy`` 只定义协议，实现放在这里——依赖方向必须是
``tools`` ← ``runtime``，反过来会让工具层为了发一个审批请求而依赖整条运行时。

四种实现对应四种部署形态：

============== ================================================
实现            场景
============== ================================================
AutoApprover   本地开发，不想每次都被打断
DenyApprover   服务化的**安全默认值**：没人看着就别放行
ConsoleApprover CLI，在终端里问一句
QueueApprover  SSE 服务：把请求发给客户端，等它 POST 决定回来
============== ================================================
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

from ..tools.policy import ApprovalDecision, ApprovalRequest

__all__ = [
    "AutoApprover",
    "CallbackApprover",
    "ConsoleApprover",
    "DenyApprover",
    "QueueApprover",
]


class AutoApprover:
    """全部放行。

    只适合本地开发。放进生产就是把安全策略整个关掉了——所以它的类名刻意叫
    ``Auto`` 而不是 ``AllowAll``，让 code review 时更容易被注意到。
    """

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.allow("自动放行")


class DenyApprover:
    """全部拒绝。服务化场景的默认值。

    「默认拒绝」而不是「默认放行」：没有人看着的时候，一个 agent 自主执行的
    写操作或命令，出了事没人能及时拦。宁可让任务失败并把这一步交回给人。
    """

    def __init__(self, reason: str = "当前会话未开启审批通道") -> None:
        self._reason = reason

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision.deny(self._reason)


class CallbackApprover:
    """把一个回调函数包成审批通道。

    回调可以是同步的也可以是异步的，接收 :class:`ApprovalRequest`，
    返回 :class:`ApprovalDecision` 或者布尔值。
    """

    def __init__(
        self, callback: Callable[[ApprovalRequest], object]
    ) -> None:
        self._callback = callback

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        outcome = self._callback(request)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome

        if isinstance(outcome, ApprovalDecision):
            return outcome
        return ApprovalDecision.allow() if outcome else ApprovalDecision.deny()


class ConsoleApprover:
    """在终端里提问。

    ``input()`` 是阻塞调用，必须丢进线程执行——直接在事件循环里调它会卡住
    所有并发的 run，也会让「取消」在等待期间失效。
    """

    def __init__(self, *, timeout: float | None = 120.0) -> None:
        self._timeout = timeout

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        prompt = self._format(request)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(input, prompt), timeout=self._timeout
            )
        except TimeoutError:
            return ApprovalDecision.deny(f"等待审批超时（{self._timeout:g} 秒）")

        # 提示语写的是 ``[y/N]``，所以空输入必须是**拒绝**。
        # 安全提示上的默认值不能反过来——一次误敲回车就放行一个写操作，
        # 这个方向的错误没法用「方便」来辩解。
        normalized = answer.strip().lower()
        if normalized in ("y", "yes"):
            return ApprovalDecision.allow("用户在终端批准")
        return ApprovalDecision.deny("用户在终端拒绝或未明确批准")

    @staticmethod
    def _format(request: ApprovalRequest) -> str:
        lines = ["", "─" * 60, "⚠️  需要审批", f"  工具: {request.tool_name}"]
        if request.command:
            lines.append(f"  命令: {request.command}")
        lines.append(f"  原因: {request.reason}")
        lines.append("─" * 60)
        lines.append("批准执行？[y/N] > ")
        return "\n".join(lines)


class QueueApprover:
    """把审批请求交给外部决定，用 ``asyncio.Future`` 等待。

    SSE 场景用的就是它：

    1. 引擎发一个 ``ApprovalRequested`` 事件出去（由 :class:`EventBus` 完成）；
    2. 客户端看到后 POST 一个决定回来；
    3. HTTP 路由调 :meth:`resolve`，对应的 Future 完成，工具继续执行。

    用 ``tool_use_id`` 做关联键——一次 run 里可能同时挂着多个待审批动作。
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    @property
    def pending_ids(self) -> list[str]:
        return [key for key, future in self._pending.items() if not future.done()]

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        key = request.tool_use_id or f"{request.tool_name}:{id(request)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[key] = future

        try:
            return await future
        finally:
            self._pending.pop(key, None)

    def resolve(
        self,
        tool_use_id: str,
        decision: ApprovalDecision | bool,
        note: str = "",
    ) -> bool:
        """外部做出决定。返回 ``False`` 表示没有这个待审批项（可能已超时或已处理）。"""
        future = self._pending.get(tool_use_id)
        if future is None or future.done():
            return False

        if isinstance(decision, ApprovalDecision):
            future.set_result(decision)
        else:
            future.set_result(
                ApprovalDecision.allow(note) if decision else ApprovalDecision.deny(note)
            )
        return True


def default_console_approver() -> ConsoleApprover | None:
    """CLI 场景的默认审批通道。

    只有在 stdin 是终端时才启用——如果输出被重定向到管道，``input()`` 会读到
    EOF 并立刻返回空串，那等价于「默默全部拒绝」，比明说没有人审批更让人困惑。
    """
    if not sys.stdin.isatty():
        return None
    return ConsoleApprover()
