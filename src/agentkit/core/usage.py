"""Token 用量与成本记账。

这个模块存在的理由是各家 usage 字段**语义不统一**，而不是命名不统一：

* OpenAI 系（含 DeepSeek / Qwen / Kimi / GLM）用 ``prompt_tokens`` / ``completion_tokens``，
  缓存命中数在 ``prompt_tokens_details.cached_tokens``（有的厂商放在顶层 ``cached_tokens``），
  **它是 ``prompt_tokens`` 的子集**。
* Anthropic 用 ``input_tokens`` / ``output_tokens`` /
  ``cache_read_input_tokens``，命名完全不同。

同样是子集关系的还有 reasoning token：它是 ``completion_tokens`` 的一部分。

**不做子集相减就会重复计费**——缓存命中的部分按 1/10 价计费，如果既算进全价
输入又算一遍缓存价，成本会虚高。所以 :class:`Usage` 明确记录「原值」和「计费值」两套。
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Usage:
    """一次（或累计的）token 用量。

    所有字段都是**报告口径的原始值**。子集关系由属性方法换算，调用方无需自行推导。
    """

    #: 输入 token 总数（含缓存命中的部分）。
    input_tokens: int = 0
    #: 输出 token 总数（含 reasoning 部分）。
    output_tokens: int = 0
    #: ``input_tokens`` 的子集：命中提示缓存的输入 token。
    cached_input_tokens: int = 0
    #: ``output_tokens`` 的子集：思维链 token。
    reasoning_tokens: int = 0

    @property
    def billable_input_tokens(self) -> int:
        """按全价计费的输入 token。缓存命中的部分已扣除。"""
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def billable_output_tokens(self) -> int:
        return self.output_tokens

    @property
    def total_tokens(self) -> int:
        """上下文占用量。注意不是「计费量」。"""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def scaled(self, factor: int) -> Usage:
        return replace(
            self,
            input_tokens=self.input_tokens * factor,
            output_tokens=self.output_tokens * factor,
            cached_input_tokens=self.cached_input_tokens * factor,
            reasoning_tokens=self.reasoning_tokens * factor,
        )


ZERO_USAGE = Usage()


@dataclass(frozen=True)
class Pricing:
    """每百万 token 的价格。

    单位为 :attr:`currency` 指定的货币。DeepSeek 的定价以人民币计价且有**峰谷价**
    （峰时约为闲时的 2 倍），这里只表达单一费率，峰谷差异留待 Phase 2 处理。
    """

    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float
    currency: str = "CNY"


def estimate_cost(usage: Usage, pricing: Pricing) -> float:
    """按 :class:`Pricing` 估算一次用量的花费。"""
    return (
        usage.billable_input_tokens * pricing.input_per_mtok
        + usage.cached_input_tokens * pricing.cached_input_per_mtok
        + usage.billable_output_tokens * pricing.output_per_mtok
    ) / 1_000_000


#: 内置价格表。
#:
#: ⚠️ **这些数字未经官方核对**，来自 2026-09 的第三方价格页。使用前请对照
#: https://api-docs.deepseek.com/quick_start/pricing/ 核实——价格是会变的，
#: 而且 DeepSeek 有峰谷价（此处取闲时价）。生产环境应当从配置注入。
DEFAULT_PRICING: dict[str, Pricing] = {
    "deepseek-flash": Pricing(
        input_per_mtok=0.5,
        cached_input_per_mtok=0.05,
        output_per_mtok=1.5,
    ),
    "deepseek-v4-pro": Pricing(
        input_per_mtok=1.6,
        cached_input_per_mtok=0.16,
        output_per_mtok=4.8,
    ),
}


def pricing_for(model: str) -> Pricing | None:
    """按模型名查价格。查不到返回 ``None``——宁可不报成本，也不编一个数。"""
    if model in DEFAULT_PRICING:
        return DEFAULT_PRICING[model]
    # 退化处理：带日期后缀的模型名（如 deepseek-flash-2026xx）按前缀匹配
    for known, pricing in DEFAULT_PRICING.items():
        if model.startswith(known):
            return pricing
    return None
