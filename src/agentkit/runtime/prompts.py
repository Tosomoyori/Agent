"""系统提示词。

对比旧实现的 ``prompt_template.py``：那里有近 60 行在教模型**怎么输出 JSON**
（两种输出模式的格式、"必须包含 thought 字段"、示例、转义规则）。那些内容全部是为了
迁就文本解析式 ReAct 而存在的。

改用原生 function calling 之后，工具调用的格式由 API 层保证，提示词只需要说清楚
**任务与边界**。这不是「提示词变简单了」，而是把本就不该由提示词承担的职责
还给了协议层——模型的输出格式错误率从两位数降到接近零。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

DEFAULT_SYSTEM_PROMPT = """\
你是一个能调用工具来完成任务的问题解决助手。

## 环境

- 操作系统: {os_name}
- 工作区根目录: {workspace}
- 可用工具: {tool_names}

所有文件路径都相对于工作区根目录。工具无法访问工作区之外的路径。

## 工作方式

1. 需要了解文件内容时**用工具去读**，不要凭猜测作答。
2. 一次可以调用多个互不依赖的工具，它们会被依次执行。
3. 工具返回的结果如果标着错误，仔细读错误信息——它通常已经说明了怎么修正。
   参数写错时直接改正重试，不要放弃也不要换一个不相干的工具。
4. 信息足够回答时就给出最终答复。不要在已经能回答的情况下继续调用工具。
5. 如果某个操作需要人工审批而被挡下，把这一步写进最终答复告诉用户，
   而不是反复重试同一条命令。

## 作答

用与用户提问相同的语言回答，直接给出结论和依据，不要复述工具返回的原文。
"""


def render_system_prompt(
    *,
    workspace: Path,
    tool_names: list[str],
    template: str | None = None,
) -> str:
    """渲染系统提示词。"""
    return (template or DEFAULT_SYSTEM_PROMPT).format(
        os_name=f"{platform.system()} {platform.release()}",
        workspace=workspace,
        tool_names=", ".join(tool_names) if tool_names else "(无)",
        shell="cmd.exe" if sys.platform == "win32" else "/bin/sh",
    )
