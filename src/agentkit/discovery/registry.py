"""Agent 注册表：注册、管理、发现。

三个动作对应 JD 里的「Agent 注册、管理、发现能力的架构设计」：

* **注册**——登记一张卡片，同名重复注册要报错而不是静默覆盖；
* **管理**——列出、查看、注销、按版本区分；
* **发现**——按标签检索「谁能做这件事」。

**这里做的是进程内注册表，不是分布式服务注册中心。** 定位是「本地有哪些 agent
可以被调用」，配合 ``/.well-known/agent-card.json`` 让远端也能发现本进程。
真正的服务发现（健康检查、负载均衡、跨机一致性）在这个规模下是过度设计——
写了也演示不出来，还会引入一堆说不出所以然的失败模式。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from .card import AgentCard

__all__ = ["AgentRegistry", "RegistrationError"]


class RegistrationError(ValueError):
    """注册失败。"""


@dataclass
class AgentRegistry:
    """进程内的 agent 注册表。"""

    #: 注册表自己的元信息，会出现在根卡片的 extensions 里。
    title: str = "AgentKit registry"
    _agents: dict[str, AgentCard] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ 注册

    def register(self, card: AgentCard, *, replace: bool = False) -> AgentCard:
        """登记一张卡片。

        重名默认**报错**而不是覆盖：静默覆盖会让「我明明注册了新版本，怎么还是
        旧的在跑」这类问题极难排查。要覆盖就显式传 ``replace=True``。
        """
        if card.name in self._agents and not replace:
            existing = self._agents[card.name]
            raise RegistrationError(
                f"agent {card.name!r} 已经注册过（版本 {existing.version}）。"
                "要覆盖请显式传 replace=True。"
            )
        self._agents[card.name] = card
        return card

    def unregister(self, name: str) -> bool:
        """注销。返回是否真的删掉了。"""
        return self._agents.pop(name, None) is not None

    # ------------------------------------------------------------ 管理

    def get(self, name: str) -> AgentCard | None:
        return self._agents.get(name)

    def all(self) -> list[AgentCard]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents)

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __iter__(self) -> Iterator[AgentCard]:
        return iter(self._agents.values())

    # ------------------------------------------------------------ 发现

    def discover(self, *tags: str, require_all: bool = False) -> list[AgentCard]:
        """按能力标签找 agent。

        没有给标签时返回全部——「发现」在无约束条件下就是「列出全部」。
        """
        if not tags:
            return self.all()
        return [
            card for card in self._agents.values()
            if card.matches(*tags, require_all=require_all)
        ]

    def discover_one(self, *tags: str, require_all: bool = False) -> AgentCard | None:
        """找一个 agent。

        匹配到多个时返回 ``None`` 而不是随便挑一个——「随便挑」会把一个本可以被
        调用方处理的歧义，变成一次看起来正常但结果不对的调用。
        """
        matches = self.discover(*tags, require_all=require_all)
        return matches[0] if len(matches) == 1 else None

    def ambiguous(self, *tags: str) -> list[AgentCard]:
        """匹配到多个时返回全部，便于调用方消歧。"""
        return self.discover(*tags)

    # ------------------------------------------------------------ 序列化

    def to_well_known(self) -> dict[str, object]:
        """整个注册表的目录，供接口暴露。"""
        return {
            "title": self.title,
            "count": len(self._agents),
            "agents": [card.to_well_known() for card in self._agents.values()],
        }
