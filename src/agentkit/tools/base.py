"""工具协议与参数 schema 生成。

一个工具需要向模型暴露的只有三样东西：名字、描述、参数的 JSON Schema。手写 schema
既啰嗦又容易和实现对不上，所以这里**从函数签名自动生成**：

.. code-block:: python

    @registry.tool(description="读取文件内容")
    async def read_file(
        ctx: ToolContext,
        file_path: Annotated[str, "相对于工作区的文件路径"],
    ) -> str:
        ...

``Annotated`` 里的字符串会成为该字段的 description——模型主要靠它和字段名决定
怎么填参数，这是提示词工程里性价比最高的一处。

第一个参数固定是 :class:`ToolContext`（依赖注入），**不进 schema**。旧实现用模块级
全局变量 ``WORKSPACE`` 存工作区路径，导致一个进程只能有一个工作区、也没法并发跑
多个 run；改成显式传递后这个限制消失了。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, create_model

from ..core.errors import ToolValidationError
from ..core.types import ToolSchema
from .policy import Approver, resolve_in_workspace

__all__ = ["ToolContext", "ToolSpec", "ContextAwareCallable", "build_args_model"]


@dataclass
class ToolContext:
    """工具执行时的上下文。由 runtime 构造并注入，不来自模型的参数。"""

    #: 工具可以读写的工作区根目录。
    workspace: Path
    #: 当前 run 的 id，用于日志关联。
    run_id: str = ""
    #: 当前这次工具调用的 id。由注册表在每次调用前填好——审批要靠它把
    #: 「提出请求」和「收到决定」关联起来。
    tool_use_id: str = ""
    #: 审批通道。``None`` 表示没有通道——此时需要审批的动作会被拒绝而不是放行。
    #: 类型是 ``tools.policy`` 里的协议，具体实现由 runtime 注入。
    approver: Approver | None = None
    #: 供工具之间共享的进程内状态。刻意保留为显式字段，避免再引入模块级全局变量。
    state: dict[str, Any] = field(default_factory=dict)

    async def request_approval(self, request) -> bool:
        """走审批通道。没有通道时**默认拒绝**——不是默认放行。

        这里会**替工具补上 ``tool_use_id``**。审批的请求与决定靠它关联：工具作者
        忘了填，服务端就会用一个客户端无从得知的兜底 key 挂起，那个审批永远等不到
        回应，run 也就无声地卡死。

        （这不是假想——最初的 ``run_command`` 就漏了它，实测起服务后跑一个需要
        审批的命令，run 直接挂住了。宁可在这里补，也不指望每个工具作者都记得。）
        """
        if self.approver is None:
            return False

        if not request.tool_use_id and self.tool_use_id:
            request.tool_use_id = self.tool_use_id

        decision = await self.approver.request(request)
        return decision.approved

    def resolve(self, path: str | Path) -> Path:
        """把路径解析成工作区内的真实路径，越界时抛 :class:`PathViolation`。"""
        return resolve_in_workspace(self.workspace, path)

    def display_path(self, path: Path) -> str:
        """尽量用相对工作区的路径来展示，日志更短也更好读。"""
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)


ContextAwareCallable = Callable[..., Awaitable[str]]


def _split_annotated(annotation: Any) -> tuple[Any, str | None]:
    """拆开 ``Annotated[T, "描述"]``，返回 ``(T, 描述)``。"""
    if get_origin(annotation) is not Annotated:
        return annotation, None

    args = get_args(annotation)
    base = args[0]
    description = None
    for meta in args[1:]:
        # 只认字符串形式；Field(...) 之类留给 pydantic 自己处理
        if isinstance(meta, str) and description is None:
            description = meta
    return base, description


def build_args_model(fn: ContextAwareCallable, *, name: str | None = None) -> type[BaseModel]:
    """从函数签名生成 pydantic 参数模型。

    第一个参数（:class:`ToolContext`）不参与 schema。``*args`` / ``**kwargs`` 不受支持
    ——模型没法给它们填参数，静默忽略只会让 schema 和实现对不上，不如直接报错。
    """
    signature = inspect.signature(fn)

    try:
        # include_extras=True 才能保住 Annotated 里的描述
        hints = get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - 前向引用解析失败时退化成原始注解
        hints = {}

    parameters = list(signature.parameters.values())[1:]  # 跳过 ctx

    model_fields: dict[str, tuple[Any, Any]] = {}
    for param in parameters:
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(
                f"工具 {fn.__name__!r} 的参数 {param.name!r} 是可变参数，"
                "无法生成 JSON Schema——请改成显式的具名参数。"
            )

        annotation = hints.get(param.name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = str

        annotation, description = _split_annotated(annotation)

        default = ... if param.default is inspect.Parameter.empty else param.default
        model_fields[param.name] = (annotation, Field(default, description=description))

    return create_model(name or f"{fn.__name__}Args", **model_fields)


@dataclass
class ToolSpec:
    """一个工具对内的完整描述。"""

    name: str
    description: str
    args_model: type[BaseModel]
    fn: ContextAwareCallable

    #: 该工具是否会产生副作用。Phase 2 的审批与幂等键依赖这个标记。
    dangerous: bool = False
    #: 同样的入参重复执行是否安全。重放 checkpoint 时用它决定能否跳过。
    idempotent: bool = True

    def json_schema(self) -> dict[str, Any]:
        """参数的 JSON Schema。"""
        schema = self.args_model.model_json_schema()
        # 顶层 title 是 pydantic 加的，对模型没有信息量
        schema.pop("title", None)
        return schema

    def schema(self) -> ToolSchema:
        """转成 provider 中立的形式。"""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.json_schema(),
        )

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        """校验模型给的参数。

        失败时抛 :class:`ToolValidationError`，且错误信息**指名字段**——
        这条信息会被回灌给模型，写得含糊它就没法修正。
        """
        from pydantic import ValidationError as PydanticValidationError

        try:
            return self.args_model.model_validate(arguments)
        except PydanticValidationError as exc:
            problems = []
            for error in exc.errors():
                location = ".".join(str(p) for p in error["loc"]) or "(根)"
                problems.append(f"  - {location}: {error['msg']}")
            raise ToolValidationError(
                f"工具 {self.name!r} 的参数不合法：\n" + "\n".join(problems)
            ) from exc

    @classmethod
    def from_function(
        cls,
        fn: ContextAwareCallable,
        *,
        name: str | None = None,
        description: str | None = None,
        dangerous: bool = False,
        idempotent: bool = True,
    ) -> ToolSpec:
        """从一个 ``async def`` 函数构造工具。

        描述优先取显式传入的，其次取 docstring 的**第一行**——整段 docstring 通常
        是为人类读者写的，塞进 schema 只是白烧 token。
        """
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"工具 {fn.__name__!r} 必须是 async 函数")

        if description is None:
            doc = inspect.getdoc(fn) or ""
            description = doc.strip().split("\n")[0].strip()

        if not description:
            raise ValueError(
                f"工具 {fn.__name__!r} 没有描述。模型靠描述决定什么时候调用它——"
                "显然缺了它工具就不会被正确使用。"
            )

        return cls(
            name=name or fn.__name__,
            description=description,
            args_model=build_args_model(fn),
            fn=fn,
            dangerous=dangerous,
            idempotent=idempotent,
        )
