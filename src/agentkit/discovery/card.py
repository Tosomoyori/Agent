"""AgentCard —— 描述一个 agent 能做什么。

字段对齐 **A2A (Agent2Agent) 协议** 的 Agent Card。A2A 由 Google 在 2025 年发起，
已捐给 Linux Foundation，2026 年转入 Agentic AI Foundation（与 MCP 同属一个基金会）。
它的定位和 MCP 互补：

* **MCP 管 agent ↔ 工具**（垂直：一个 agent 内部怎么用工具）；
* **A2A 管 agent ↔ agent**（水平：一个 agent 怎么发现并调用另一个 agent）。

规范路径是 ``/.well-known/agent-card.json``——把「去哪里问一个服务它能做什么」
标准化成了一句约定的 URL，这是整个发现机制的基础。

**明确没做**：v1.0 的招牌特性是签名 Agent Card（JCS 规范化 + Ed25519 detached JWS），
以及 JSON-RPC 服务端、webhook 推送、``tasks/resubscribe``。这些是数周的工作量，
而且签名在控制台 demo 里一点都展示不出来。取舍写在 ``docs/a2a-scope.md``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["AgentSkill", "AgentCard", "AgentProvider", "AgentCapabilities"]


class AgentProvider(BaseModel):
    """谁提供了这个 agent。"""

    organization: str = ""
    url: str = ""


class AgentCapabilities(BaseModel):
    """协议层面的能力开关。

    客户端据此决定能不能用流式、能不能靠推送。写死成 ``True`` 是不负责任的——
    声明了却做不到，客户端会在运行到一半时失败。
    """

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentSkill(BaseModel):
    """一项能力。发现机制真正匹配的对象是它，不是 agent 本身。

    ``tags`` 与 ``examples`` 供**调用方**（人或另一个 agent）判断能力匹配度。
    """

    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])


class AgentCard(BaseModel):
    """一个 agent 的自述。"""

    name: str
    description: str = ""
    url: str = ""
    version: str = "0.1.0"

    #: 协议版本。对齐 A2A 的 ``protocolVersion``。
    protocol_version: str = "1.0"
    #: 首选传输方式。A2A 的主绑定是 JSON-RPC over HTTP。
    preferred_transport: str = "JSONRPC"

    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    provider: AgentProvider = Field(default_factory=AgentProvider)
    skills: list[AgentSkill] = Field(default_factory=list)

    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])

    #: 供本项目扩展用的字段，A2A 允许实现方加自己的键。
    extensions: dict[str, Any] = Field(default_factory=dict)

    #: 模型 id，方便调用方判断成本和能力。
    model: str = ""

    # ------------------------------------------------------------ 查询

    def skill(self, skill_id: str) -> AgentSkill | None:
        for item in self.skills:
            if item.id == skill_id:
                return item
        return None

    def matches(self, *tags: str, require_all: bool = False) -> bool:
        """按标签判断这个 agent 是否具备某项能力。

        没有技能的 agent 用默认输入输出模式兜底（它能处理文本，仅此而已）。
        """
        if not tags:
            return True

        # 两边都要归一化。只转一边的话，`"Code" in ["code"]` 会返回 False——
        # 标签大小写本来就不该有语义，一致性得自己保证。
        available: set[str] = set()
        for item in self.skills:
            available.update(tag.lower() for tag in item.tags)

        wanted = {t.lower() for t in tags}
        if require_all:
            return wanted <= available
        return bool(wanted & available)

    # ------------------------------------------------------------ 序列化

    def to_well_known(self) -> dict[str, Any]:
        """转成 ``/.well-known/agent-card.json`` 的形状。

        字段名用 camelCase，因为 A2A 的 schema 是 camelCase 的——
        这个转换必须在这里做，不能指望消费方去猜。
        """
        payload: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "preferredTransport": self.preferred_transport,
            "capabilities": {
                "streaming": self.capabilities.streaming,
                "pushNotifications": self.capabilities.push_notifications,
                "stateTransitionHistory": self.capabilities.state_transition_history,
            },
            "defaultInputModes": self.default_input_modes,
            "defaultOutputModes": self.default_output_modes,
            "skills": [
                {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                    "tags": skill.tags,
                    "examples": skill.examples,
                    "inputModes": skill.input_modes,
                    "outputModes": skill.output_modes,
                }
                for skill in self.skills
            ],
        }
        if self.provider.organization or self.provider.url:
            payload["provider"] = {
                "organization": self.provider.organization,
                "url": self.provider.url,
            }
        if self.extensions:
            payload["extensions"] = self.extensions
        return payload

    @classmethod
    def from_well_known(cls, payload: dict[str, Any]) -> AgentCard:
        """从远端取得的卡片反序列化。

        对未知字段宽容（客户端不该因为服务端加了个新键就挂掉），
        但必需的 ``name`` 缺失时必须报错——一个没有名字的 agent 没法被引用。
        """
        capabilities = payload.get("capabilities") or {}
        provider = payload.get("provider") or {}

        skills = [
            AgentSkill(
                id=item.get("id", ""),
                name=item.get("name", ""),
                description=item.get("description", ""),
                tags=list(item.get("tags") or []),
                examples=list(item.get("examples") or []),
                input_modes=list(item.get("inputModes") or ["text/plain"]),
                output_modes=list(item.get("outputModes") or ["text/plain"]),
            )
            for item in payload.get("skills") or []
        ]

        return cls(
            name=payload["name"],
            description=payload.get("description", ""),
            url=payload.get("url", ""),
            version=payload.get("version", "0.1.0"),
            protocol_version=payload.get("protocolVersion", "1.0"),
            preferred_transport=payload.get("preferredTransport", "JSONRPC"),
            capabilities=AgentCapabilities(
                streaming=bool(capabilities.get("streaming")),
                push_notifications=bool(capabilities.get("pushNotifications")),
                state_transition_history=bool(
                    capabilities.get("stateTransitionHistory")
                ),
            ),
            provider=AgentProvider(
                organization=provider.get("organization", ""),
                url=provider.get("url", ""),
            ),
            skills=skills,
            default_input_modes=list(payload.get("defaultInputModes") or ["text/plain"]),
            default_output_modes=list(payload.get("defaultOutputModes") or ["text/plain"]),
            extensions=dict(payload.get("extensions") or {}),
            model=payload.get("model", ""),
        )


Transport = Literal["JSONRPC", "GRPC", "HTTP+JSON"]
