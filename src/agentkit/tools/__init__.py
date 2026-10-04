"""工具协议、注册表与内置工具。

这一层**不 import ``agentkit.runtime``**——审批等需要回调 runtime 的能力，
由 runtime 反向注入实现，保证依赖方向单向。
"""

from .base import ToolContext, ToolSpec, build_args_model
from .builtin import BUILTIN_GROUPS, default_registry, register_builtin_tools
from .policy import (
    DEFAULT_ALLOWED_EXECUTABLES,
    CommandAssessment,
    Verdict,
    assess_command,
    resolve_in_workspace,
)
from .registry import ToolRegistry

__all__ = [
    "DEFAULT_ALLOWED_EXECUTABLES",
    "BUILTIN_GROUPS",
    "CommandAssessment",
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "Verdict",
    "assess_command",
    "build_args_model",
    "default_registry",
    "register_builtin_tools",
    "resolve_in_workspace",
]
