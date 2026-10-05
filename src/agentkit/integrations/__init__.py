"""与其他框架/生态的互操作层。

这些模块都是**可选依赖**——核心项目不需要它们。只有真的 import 时才要求
对应的包已安装::

    uv sync --extra langchain
    from agentkit.integrations.langchain import AgentKitChatModel

之所以做成惰性导入而不是顶层 re-export，是因为这里有一条硬约束：
**核心包不能因为别人没装 LangChain 就 import 失败**。
"""

from __future__ import annotations

__all__ = ["langchain"]


def __getattr__(name: str):
    if name == "langchain":
        from . import langchain

        return langchain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
