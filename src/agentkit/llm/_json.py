"""工具参数的 JSON 处理。

抽成独立模块是因为**两处**都要用它：适配器解析完整响应时，以及流式累加器
在收尾时校验拼出来的参数。放在任何一侧都会造成反向依赖。
"""

from __future__ import annotations

import json
from typing import Any


def is_complete_json_object(text: str) -> bool:
    """``text`` 是不是一个完整的 JSON **对象**。

    刻意只认 ``dict`` 而不认「任意合法 JSON」：``json.loads("8")`` 是合法的，
    但那只是一个数字片段。用它判断「工具参数是否拼完整了」会得出错误结论——
    实测中 ``arguments`` 的分片里就出现过单独的 ``"8"``。
    """
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def parse_tool_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """解析模型给出的工具参数。

    返回 ``(参数, 无法解析的原文)``，**不抛异常**。

    理由：「模型给了一段不是 JSON 的参数」是可恢复的错误——把它当作一次参数校验
    失败回灌给模型，让它修正重试，比中断整个 run 划算。所以这里把原文一并交出去，
    由上层决定怎么在回灌消息里说明。

    模型有时用 ``null`` 或空串表示「这个工具没有参数」，那不是错误。
    """
    if raw is None:
        return {}, None

    text = raw.strip()
    if not text or text.lower() in ("null", "none", "{}"):
        return {}, None

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}, raw

    if isinstance(value, dict):
        return value, None

    # 合法 JSON 但不是对象（比如模型直接给了个字符串或数组）
    return {}, raw


def join_argument_fragments(fragments: list[str]) -> tuple[str, str | None]:
    """把流式 ``arguments`` 片段拼成完整字符串。

    返回 ``(拼出的文本, 备注)``。

    这里要应对两种 delta 语义，**且不能猜错**：

    * **增量**（实测 ``deepseek-flash`` 的行为）：每片是新内容，直接拼接。
    * **累积重发**（部分 provider 如百度千帆）：每片重发「迄今为止的全部内容」，
      无脑拼接会得到 ``{...}{...}``，``json.loads`` 失败后被静默吞成空参数。

    做法是先用增量拼接，拼出来不是合法对象时再试「取最长片段」这条退路，
    并把走了退路这件事**记在备注里**——静默地做对，比做错了还让人放心不下。
    """
    if not fragments:
        return "", None

    joined = "".join(fragments)
    if is_complete_json_object(joined):
        return joined, None

    longest = max(fragments, key=len)
    if longest != joined and is_complete_json_object(longest):
        return longest, "检测到累积重发式 delta，已改用最长片段"

    # 两头都不是合法对象——原样交出去，让上层按「参数不是合法 JSON」回灌给模型
    return joined, None
