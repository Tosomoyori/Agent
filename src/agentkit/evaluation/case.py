"""评测用例的定义。

## 为什么评的是**执行后的状态**，不是对话记录

最省事的评测方式是拿最终答复跟参考答案做字符串比对。它有两个问题：

1. **措辞不同就算错。** 「有 43 个文件」和「src/agentkit 下共有 43 个 .py 文件」
   是同一个答案，字符串比对会判错；
2. **过程对、结果错。** 模型可能说「我已经把文件写好了」，而实际上什么都没写。
   比对答复永远发现不了这种。

所以主判据是 **checks**——它们检查工作区的实际状态（文件在不在、内容对不对）
和最终答复里的**关键事实**（不是逐字匹配）。这个思路来自 τ-bench：按最终数据库
状态评分，而不是按 transcript 评分。

字符串匹配只在 ``answer_contains`` 里用，而且约束的是**关键事实**
（比如那个数字），不是整句话。

## hermetic 与 live 两层

``script`` 字段是可选的：给了它，这个用例就能在 **hermetic 模式**下跑——
用一个按脚本返回的假模型，不联网、不花钱、结果确定。作用不是评测 agent 的能力，
而是**评测评测框架自己**：CI 里必须能跑，否则一套没人跑的评测等于没有。

agent 的真实能力只能在 live 模式下测，那要真花钱。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["CheckSpec", "ScriptStep", "EvalCase", "load_cases", "dump_cases"]


class CheckSpec(BaseModel):
    """一条判据。

    声明式而不是可执行代码：JSONL 里写一段 Python 表达式很诱人，但那样用例就不再
    是数据了——它不能被别的工具读、没法做静态分析、还会成为注入面。
    """

    type: Literal[
        "answer_contains",
        "answer_not_contains",
        "file_exists",
        "file_missing",
        "file_contains",
        "file_not_contains",
        "dir_exists",
        "dir_missing",
        "tools_called",
        "tools_not_called",
        "tool_args_include",
        "no_failed_tools",
        "max_steps",
    ]

    #: 涉及路径的判据用
    path: str | None = None
    #: 涉及子串的判据用。
    #:
    #: ⚠️ **``answer_not_contains`` 是个陷阱，用之前先想清楚。**
    #:
    #: 它做的是字面子串匹配，所以「不能出现 3」会命中「13」，「不能出现 E402」
    #: 会命中「E4021」。更要命的是它在检查**措辞**而不是**事实**——
    #: 「core.py 不以 test_ 开头」这种正确回答会因为提到了 core.py 而被判错。
    #:
    #: 本项目写第一批用例时连踩三次这个坑。经验：能用**正面判据**（必须出现某个
    #: 关键事实）表达的，就别用负面判据；非用不可时，只列那些**绝无可能作为
    #: 正确内容的一部分出现**的词。
    contains: list[str] = Field(default_factory=list)
    #: 涉及工具的判据用
    tools: list[str] = Field(default_factory=list)
    #: ``tools_called`` 列了多个工具时，是「都要调」还是「调任意一个即可」。
    #:
    #: 这个字段是必须的：两种读法都说得通，猜错了会得到一个看起来在工作、
    #: 实际在测别的东西的判据。默认 ``all``——从严。
    match: Literal["all", "any"] = "all"
    #: ``tool_args_include`` 用：期望某个工具的入参包含这些键值
    args: dict[str, Any] = Field(default_factory=dict)
    #: ``max_steps`` 用
    limit: int | None = None

    #: 判据失败时显示给人和模型的说明
    note: str = ""


class ScriptStep(BaseModel):
    """hermetic 模式下的一轮模型输出。"""

    #: 要调用的工具，``(名字, 参数)``
    call: tuple[str, dict[str, Any]] | None = None
    #: 直接给最终答复
    answer: str | None = None


class EvalCase(BaseModel):
    """一个评测用例。"""

    id: str
    prompt: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    #: 运行前写进工作区的文件。评测要可复现，所以环境由用例自己带来，
    #: 不依赖仓库当前长什么样。
    setup_files: dict[str, str] = Field(default_factory=dict)

    #: 判据。全通过才算这个用例通过。
    checks: list[CheckSpec] = Field(default_factory=list)

    #: hermetic 模式用的脚本。为空表示这个用例只能在 live 模式下跑。
    script: list[ScriptStep] = Field(default_factory=list)

    #: 参与聚合时的权重。默认 1.0。
    weight: float = 1.0

    @property
    def hermetic_ready(self) -> bool:
        return bool(self.script)

    def model_dump_jsonl(self) -> str:
        import json

        return json.dumps(self.model_dump(exclude_defaults=True), ensure_ascii=False)


def load_cases(path) -> list[EvalCase]:
    """从 JSONL 文件读用例。一行一个。

    选 JSONL 而不是 YAML：一是少一个依赖，二是**一行一个用例**意味着 diff 里
    一眼能看出改了哪个用例，追加新用例也不会产生整文件的 diff。
    """
    import json
    from pathlib import Path

    target = Path(path)
    files = sorted(target.glob("*.jsonl")) if target.is_dir() else [target]

    cases: list[EvalCase] = []
    for file in files:
        for lineno, line in enumerate(
            file.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            try:
                cases.append(EvalCase.model_validate(json.loads(stripped)))
            except Exception as exc:
                raise ValueError(f"{file.name} 第 {lineno} 行解析失败: {exc}") from exc

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            # id 重复会让报告里的数字对不上号，直接拒绝
            raise ValueError(f"用例 id 重复: {case.id!r}")
        seen.add(case.id)

    return cases


def dump_cases(cases: list[EvalCase], path) -> None:
    from pathlib import Path

    Path(path).write_text(
        "\n".join(case.model_dump_jsonl() for case in cases) + "\n", encoding="utf-8"
    )
