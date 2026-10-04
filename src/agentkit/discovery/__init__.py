"""Agent 注册、管理与发现。

卡片字段对齐 A2A 协议的 Agent Card。刻意只做骨架——签名卡片、JSON-RPC 服务端、
webhook 推送都是明确划出的非目标，理由见 ``docs/a2a-scope.md``。
"""

from .card import AgentCapabilities, AgentCard, AgentProvider, AgentSkill
from .registry import AgentRegistry, RegistrationError

__all__ = [
    "AgentCapabilities",
    "AgentCard",
    "AgentProvider",
    "AgentRegistry",
    "AgentSkill",
    "RegistrationError",
]
