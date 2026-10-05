"""模型能力目录。

DeepSeek 侧的数据来自 2026-10-05 实测 ``GET https://api.deepseek.com/models``：

.. code-block:: text

    deepseek-flash    DeepSeek-V4.1-Flash  ctx=1048576  max_out=393216  in=[text,image]
    deepseek-v4-pro   DeepSeek-V4-Pro      ctx=1048576  max_out=393216  in=[text]

两者均支持 ``effort`` 档位（low / high / max，默认 high）。

**模型能力必须以可查数据的形式存在。** 模型名与能力会随上游变更，
将其散落在代码中的条件分支里会导致升级时出现难以定位的失效。
本目录是唯一的查询入口，且允许调用方覆盖。
"""

from __future__ import annotations

from dataclasses import replace

from .base import ModelCapabilities

__all__ = ["KNOWN_MODELS", "capabilities_for", "is_known_model"]

#: 已知模型的实测能力。
KNOWN_MODELS: dict[str, ModelCapabilities] = {
    "deepseek-flash": ModelCapabilities(
        context_window=1_048_576,
        max_output_tokens=393_216,
        tool_calling=True,
        parallel_tool_calls=True,
        streaming=True,
        streaming_tool_calls=True,
        json_object=True,
        # 有 effort 档位（low/high/max），走 extra_body 传
        reasoning=True,
        image_input=True,
        # 实测：不传 stream_options 也会下发 usage（OpenAI 需要显式开）
        usage_in_stream=True,
    ),
    "deepseek-v4-pro": ModelCapabilities(
        context_window=1_048_576,
        max_output_tokens=393_216,
        tool_calling=True,
        parallel_tool_calls=True,
        streaming=True,
        streaming_tool_calls=True,
        json_object=True,
        reasoning=True,
        image_input=False,
        usage_in_stream=True,
    ),
}

#: 查不到时用的保守默认值。
#:
#: 刻意把 ``parallel_tool_calls`` 和 ``streaming_tool_calls`` 设成 ``False``：
#: 对一个未知模型，宁可少用特性也不要假设它有。Qwen 的 ``parallel_tool_calls``
#: 默认就是关的，正是这类假设会踩的坑。
FALLBACK_CAPABILITIES = ModelCapabilities(
    context_window=128_000,
    max_output_tokens=8_192,
    tool_calling=True,
    parallel_tool_calls=False,
    streaming=True,
    streaming_tool_calls=False,
    json_object=False,
    json_schema=False,
    reasoning=False,
    image_input=False,
)


def is_known_model(model: str) -> bool:
    return model in KNOWN_MODELS


def capabilities_for(model: str, **overrides: object) -> ModelCapabilities:
    """查模型能力。未知模型返回保守默认值，并允许调用方覆盖。"""
    base = KNOWN_MODELS.get(model, FALLBACK_CAPABILITIES)
    return replace(base, **overrides) if overrides else base  # type: ignore[arg-type]
