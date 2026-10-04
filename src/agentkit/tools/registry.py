"""工具注册表。

与旧实现的 ``ToolRegistry`` 相比，三处关键差别：

1. **不持有全局状态**。工具挂在实例上，一个进程可以并存多套工具集（不同权限、
   不同 agent），测试也不必担心用例之间互相污染。
2. **参数校验失败会回灌给模型**。旧代码 ``tool_func(**params)`` 直接把异常转成
   字符串，模型看不出自己错在哪。现在校验失败返回一条**指名字段**的错误结果，
   模型能据此自我修正——这是原生 tool calling 相对文本 ReAct 最实用的一个优势。
3. **不需要审批通道的工具调用开销恒定**。``invoke`` 永不抛异常（取消除外），
   所有失败都变成 ``is_error=True`` 的结果块回流给模型。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from ..core.errors import (
    AgentKitError,
    PolicyError,
    RunCancelled,
    ToolError,
)
from ..core.types import ToolResultBlock, ToolSchema
from .base import ContextAwareCallable, ToolContext, ToolSpec

__all__ = ["ToolRegistry"]


class ToolRegistry:
    """一组工具的容器。"""

    def __init__(self, tools: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in tools or []:
            self.add(spec)

    # ------------------------------------------------------------ 注册

    def add(self, spec: ToolSpec) -> ToolSpec:
        """注册一个已经构造好的工具。重名会报错而不是静默覆盖。"""
        if spec.name in self._tools:
            raise ValueError(f"工具名重复: {spec.name!r}。请显式改名或先移除旧的那个。")
        self._tools[spec.name] = spec
        return spec

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def register(self, fn: ContextAwareCallable, **kwargs: Any) -> ToolSpec:
        """从一个 ``async def`` 函数注册工具。"""
        return self.add(ToolSpec.from_function(fn, **kwargs))

    def tool(self, **kwargs: Any):
        """装饰器形式：既注册工具，又返回原函数。

        .. code-block:: python

            @registry.tool(description="读取文件内容")
            async def read_file(ctx: ToolContext, file_path: str) -> str: ...
        """

        def decorator(fn: ContextAwareCallable) -> ContextAwareCallable:
            self.register(fn, **kwargs)
            return fn

        return decorator

    def merge(self, other: ToolRegistry, *, override: bool = False) -> ToolRegistry:
        """合并另一个注册表，返回新实例（不修改双方）。"""
        merged = ToolRegistry(list(self._tools.values()))
        for name, spec in other._tools.items():  # noqa: SLF001 - 同类内部访问
            if override:
                merged._tools[name] = spec  # noqa: SLF001
            else:
                merged.add(spec)
        return merged

    # ------------------------------------------------------------ 查询

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[ToolSchema]:
        return [spec.schema() for spec in self._tools.values()]

    def describe(self) -> str:
        """给人类看（和给系统提示词用）的工具清单。"""
        return "\n".join(
            f"  - {spec.name}: {spec.description}" for spec in self._tools.values()
        )

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._tools.values())

    # ------------------------------------------------------------ 执行

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
        *,
        tool_use_id: str = "",
    ) -> ToolResultBlock:
        """校验并执行一个工具，**永不抛异常**（取消除外）。

        所有失败都变成 ``is_error=True`` 的结果块回灌给模型。取消是唯一的例外——
        它表示整个 run 该停了，不是一个可以被模型修正的错误。
        """
        started = time.perf_counter()

        def result(content: str, *, is_error: bool = False) -> ToolResultBlock:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=content,
                is_error=is_error,
            )

        spec = self.get(name)
        if spec is None:
            available = ", ".join(self.names()) or "(无)"
            return result(
                f"不存在名为 {name!r} 的工具。可用工具: {available}", is_error=True
            )

        try:
            args = spec.validate(arguments)
        except ToolError as exc:
            # 参数不合法——把问题原样告诉模型，让它自己改。这是最该走回灌路径的一类错误。
            return result(str(exc), is_error=True)

        # 复制一份上下文并填上本次调用的 id，而不是去改共享的那个 ctx——
        # 审批要靠这个 id 关联「提出请求」和「收到决定」，而共享可变状态迟早出并发问题。
        # state 字典是同一个引用，工具之间依旧可以共享进程内状态。
        call_ctx = replace(ctx, tool_use_id=tool_use_id) if tool_use_id else ctx

        try:
            output = await spec.fn(call_ctx, **args.model_dump())
        except RunCancelled:
            raise
        except (PolicyError, ToolError, AgentKitError) as exc:
            return result(f"{type(exc).__name__}: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - 工具是用户代码，什么都可能抛
            return result(
                f"工具执行异常 {type(exc).__name__}: {exc}", is_error=True
            )

        if not isinstance(output, str):
            # 工具作者忘了返回字符串。这属于实现缺陷，但要让它以可见的方式失败，
            # 而不是把奇怪的对象塞进消息里。
            return result(
                f"工具 {name!r} 返回了 {type(output).__name__} 而不是 str——"
                "这是工具实现的缺陷，请报告。",
                is_error=True,
            )

        _ = time.perf_counter() - started  # 耗时由 engine 记录，这里只保证不吞异常
        return result(output)
