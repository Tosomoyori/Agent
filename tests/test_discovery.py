"""AgentCard 与注册表。"""

from __future__ import annotations

import pytest

from agentkit.discovery import (
    AgentCapabilities,
    AgentCard,
    AgentRegistry,
    AgentSkill,
    RegistrationError,
)


def _card(name: str = "coder", *tags: str, version: str = "1.0.0") -> AgentCard:
    return AgentCard(
        name=name,
        description=f"{name} 的说明",
        url=f"http://localhost:8000/{name}",
        version=version,
        skills=[
            AgentSkill(
                id="s1",
                name="技能",
                description="干活的",
                tags=list(tags) or ["code"],
                examples=["举个例子"],
            )
        ],
    )


class TestAgentCard:
    def test_well_known_uses_camel_case(self):
        """A2A 的 schema 是 camelCase 的，转换必须在这里做，不能指望消费方去猜。"""
        payload = _card("a", "code").to_well_known()
        assert payload["protocolVersion"] == "1.0"
        assert payload["preferredTransport"] == "JSONRPC"
        assert payload["defaultInputModes"] == ["text/plain"]
        assert payload["skills"][0]["inputModes"] == ["text/plain"]

    def test_roundtrip(self):
        original = _card("coder", "code", "search")
        restored = AgentCard.from_well_known(original.to_well_known())
        assert restored.name == original.name
        assert restored.skills[0].tags == ["code", "search"]
        assert restored.version == original.version

    def test_provider_omitted_when_empty(self):
        payload = _card().to_well_known()
        assert "provider" not in payload

    def test_provider_included_when_set(self):
        card = _card()
        card.provider.organization = "agentkit"
        assert card.to_well_known()["provider"]["organization"] == "agentkit"

    def test_from_well_known_tolerates_unknown_fields(self):
        """客户端不该因为服务端加了个新键就挂掉。"""
        payload = _card().to_well_known()
        payload["somethingNew"] = {"nested": True}
        payload["skills"][0]["futureField"] = 1
        assert AgentCard.from_well_known(payload).name == "coder"

    def test_from_well_known_requires_name(self):
        """没有名字的 agent 没法被引用，这是硬性的。"""
        with pytest.raises(KeyError):
            AgentCard.from_well_known({"description": "没有名字"})

    def test_skill_lookup(self):
        card = _card()
        assert card.skill("s1") is not None
        assert card.skill("不存在") is None

    def test_capabilities_survive_roundtrip(self):
        card = _card()
        card.capabilities = AgentCapabilities(
            streaming=True, push_notifications=False, state_transition_history=True
        )
        restored = AgentCard.from_well_known(card.to_well_known())
        assert restored.capabilities.streaming is True
        assert restored.capabilities.push_notifications is False


class TestMatching:
    def test_matches_any_tag_by_default(self):
        card = _card("a", "code", "search")
        assert card.matches("code")
        assert card.matches("search")
        assert not card.matches("payments")

    def test_require_all(self):
        card = _card("a", "code", "search")
        assert card.matches("code", "search", require_all=True)
        assert not card.matches("code", "payments", require_all=True)

    def test_no_tags_matches_everything(self):
        assert _card("a", "code").matches()

    def test_case_insensitive(self):
        assert _card("a", "Code").matches("code")


class TestRegistry:
    def test_register_and_get(self):
        registry = AgentRegistry()
        registry.register(_card("coder"))
        assert registry.get("coder") is not None
        assert len(registry) == 1

    def test_duplicate_registration_raises(self):
        """静默覆盖会让「我明明注册了新版本，怎么还是旧的在跑」极难排查。"""
        registry = AgentRegistry()
        registry.register(_card("coder", version="1.0"))
        with pytest.raises(RegistrationError, match="已经注册过"):
            registry.register(_card("coder", version="2.0"))

    def test_explicit_replace(self):
        registry = AgentRegistry()
        registry.register(_card("coder", version="1.0"))
        registry.register(_card("coder", version="2.0"), replace=True)
        assert registry.get("coder").version == "2.0"

    def test_unregister(self):
        registry = AgentRegistry()
        registry.register(_card("coder"))
        assert registry.unregister("coder") is True
        assert registry.unregister("coder") is False

    def test_discover_by_tag(self):
        registry = AgentRegistry()
        registry.register(_card("coder", "code", "search"))
        registry.register(_card("biller", "payments"))

        assert [c.name for c in registry.discover("code")] == ["coder"]
        assert [c.name for c in registry.discover("payments")] == ["biller"]

    def test_discover_without_tags_lists_all(self):
        registry = AgentRegistry()
        registry.register(_card("a", "x"))
        registry.register(_card("b", "y"))
        assert len(registry.discover()) == 2

    def test_discover_one_returns_none_on_ambiguity(self):
        """匹配到多个时返回 None，而不是随便挑一个。

        「随便挑」会把一个调用方本可以处理的歧义，变成一次看起来正常但结果不对的调用。
        """
        registry = AgentRegistry()
        registry.register(_card("a", "code"))
        registry.register(_card("b", "code"))
        assert registry.discover_one("code") is None
        assert len(registry.ambiguous("code")) == 2

    def test_discover_one_returns_the_single_match(self):
        registry = AgentRegistry()
        registry.register(_card("a", "code"))
        registry.register(_card("b", "payments"))
        assert registry.discover_one("code").name == "a"

    def test_well_known_directory(self):
        registry = AgentRegistry()
        registry.register(_card("a", "code"))
        registry.register(_card("b", "payments"))
        payload = registry.to_well_known()
        assert payload["count"] == 2
        assert len(payload["agents"]) == 2

    def test_iteration(self):
        registry = AgentRegistry()
        registry.register(_card("a", "x"))
        assert [c.name for c in registry] == ["a"]
        assert "a" in registry
