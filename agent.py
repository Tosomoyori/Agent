from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import platform
from dotenv import load_dotenv
from openai import OpenAI

from prompt_template import SYSTEM_PROMPT

# ============================================================
# 配置常量
# ============================================================
MAX_STEPS = 15                   # 最大推理步数，防止无限循环
DEFAULT_TIMEOUT = 30             # 命令执行超时（秒）
DANGEROUS_PATTERNS = [           # 危险命令拦截（用正则匹配更精确）
    r"\brm\s+-rf\b",
    r"\bformat\s+[A-Za-z]:",
    r"\bshutdown\b",
    r"\brmdir\s+/s",
    r"\bdel\s+/[sqf]",
    r"\btaskkill\s+/f",
    r"\bdiskpart\b",
]

# 运行时上下文
WORKSPACE = ""  # 将在 main() 中初始化

# ============================================================
# ToolRegistry —— 装饰器式工具注册
# ============================================================
class ToolRegistry:
    """工具注册中心，使用装饰器注册工具函数"""

    _tools: Dict[str, Callable] = {}

    @classmethod
    def register(cls, name: Optional[str] = None):
        """装饰器：将函数注册为 Agent 可调用的工具"""
        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            cls._tools[tool_name] = func
            return func
        return decorator

    @classmethod
    def get(cls, name: str) -> Optional[Callable]:
        return cls._tools.get(name)

    @classmethod
    def all(cls) -> Dict[str, Callable]:
        return dict(cls._tools)

    @classmethod
    def describe_all(cls) -> str:
        """生成所有工具的描述文本"""
        lines = []
        for name, func in cls._tools.items():
            sig = _signature_str(func)
            doc = (func.__doc__ or "无描述").strip().split("\n")[0]
            lines.append(f"  - {name}{sig}: {doc}")
        return "\n".join(lines)

def _signature_str(func: Callable) -> str:
    """生成函数签名字符串"""
    import inspect
    try:
        return str(inspect.signature(func))
    except (TypeError, ValueError):
        return "()"

# ============================================================
# 安全工具函数
# ============================================================
def _ensure_safe_path(target: str) -> bool:
    """确保目标路径在工作区内"""
    if not WORKSPACE:
        return False
    abs_target = os.path.abspath(os.path.join(WORKSPACE, target)) \
        if not os.path.isabs(target) else os.path.abspath(target)
    workspace_abs = os.path.abspath(WORKSPACE)
    return abs_target == workspace_abs or abs_target.startswith(workspace_abs + os.sep)

def _abs_path(target: str) -> str:
    """将相对路径转为工作区内的绝对路径"""
    if os.path.isabs(target):
        return target
    return os.path.abspath(os.path.join(WORKSPACE, target))

# ============================================================
# 工具定义（使用 @ToolRegistry.register 装饰器）
# ============================================================
@ToolRegistry.register("read_file")
def read_file(file_path: str) -> str:
    """读取指定文件的完整内容"""
    if not _ensure_safe_path(file_path):
        return f"❌ 路径越界: {file_path}（仅限 {WORKSPACE} 内）"
    full = _abs_path(file_path)
    if not os.path.isfile(full):
        return f"❌ 文件不存在: {full}"
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        return f"📄 {full}（{line_count} 行）\n{content}"
    except UnicodeDecodeError:
        return f"❌ 非 UTF-8 编码文件，无法读取"
    except Exception as e:
        return f"❌ 读取失败: {e}"

@ToolRegistry.register("write_file")
def write_file(file_path: str, content: str) -> str:
    """将内容写入指定文件（会覆盖已有内容）"""
    if not _ensure_safe_path(file_path):
        return f"❌ 路径越界: {file_path}（仅限 {WORKSPACE} 内）"
    full = _abs_path(file_path)
    try:
        parent = os.path.dirname(full)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"✅ 已写入 {len(content)} 字符到 {full}"
    except Exception as e:
        return f"❌ 写入失败: {e}"

@ToolRegistry.register("list_directory")
def list_directory(path: str = ".") -> str:
    """列出指定目录下的文件和子目录"""
    if not _ensure_safe_path(path):
        return f"❌ 路径越界: {path}"
    full = _abs_path(path)
    if not os.path.isdir(full):
        return f"❌ 目录不存在: {full}"
    try:
        entries = sorted(os.listdir(full))
        if not entries:
            return f"📁 {full}（空目录）"
        lines = []
        for e in entries:
            full_entry = os.path.join(full, e)
            marker = "📂" if os.path.isdir(full_entry) else "📄"
            lines.append(f"  {marker} {e}")
        return f"📁 {full}（{len(entries)} 项）\n" + "\n".join(lines)
    except PermissionError:
        return f"❌ 无权限访问: {full}"
    except Exception as e:
        return f"❌ 列出失败: {e}"

@ToolRegistry.register("search_in_file")
def search_in_file(file_path: str, keyword: str) -> str:
    """在文件中搜索关键词，返回匹配行及行号"""
    if not _ensure_safe_path(file_path):
        return f"❌ 路径越界: {file_path}"
    full = _abs_path(file_path)
    if not os.path.isfile(full):
        return f"❌ 文件不存在: {full}"
    try:
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()
        matches = []
        for i, line in enumerate(lines, 1):
            if keyword.lower() in line.lower():
                matches.append(f"  L{i}: {line.rstrip()}")
        if matches:
            return f"🔍 在 {full} 中找到 {len(matches)} 处 '{keyword}':\n" + "\n".join(matches)
        return f"🔍 在 {full} 中未找到 '{keyword}'"
    except Exception as e:
        return f"❌ 搜索失败: {e}"

@ToolRegistry.register("run_command")
def run_command(command: str) -> str:
    """在工作目录中执行终端命令"""
    # 危险命令检测
    cmd_lower = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower):
            return f"⛔ 检测到危险命令模式: {pattern}"

    # 终端命令需用户确认
    confirm = input(f"\n  ⚠️  执行命令需确认 [Y/n]: {command}\n  > ").strip().lower()
    if confirm not in ("", "y", "yes"):
        return "🚫 用户取消执行"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=WORKSPACE,
            timeout=DEFAULT_TIMEOUT,
        )
        output = result.stdout.strip() or "(无标准输出)"
        err = result.stderr.strip()
        if result.returncode == 0:
            return f"✅ 执行成功\n{output}"
        return f"❌ 执行失败 (exit={result.returncode})\n{err or output}"
    except subprocess.TimeoutExpired:
        return f"⏱️ 命令超时（{DEFAULT_TIMEOUT}秒）"
    except Exception as e:
        return f"❌ 执行异常: {e}"

# ============================================================
# ReActSession —— 对话历史管理
# ============================================================
@dataclass
class ReActStep:
    """记录单次推理步骤"""
    step_num: int
    thought: str
    action: Optional[Dict[str, Any]] = None
    observation: Optional[str] = None
    final_answer: Optional[str] = None

@dataclass
class ReActSession:
    """完整的 ReAct 会话状态"""
    system_prompt: str
    user_question: str
    steps: List[ReActStep] = field(default_factory=list)

    def to_messages(self) -> List[Dict[str, str]]:
        """转换为 LLM API 需要的 messages 格式"""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"用户问题：{self.user_question}"},
        ]
        for step in self.steps:
            if step.final_answer:
                # 最后一步的 final_answer 不需要追加到 messages
                continue
            # 把 assistant 的 thought + action 拼接成一条
            assistant_msg = self._format_assistant_msg(step)
            messages.append({"role": "assistant", "content": assistant_msg})
            if step.observation is not None:
                messages.append({"role": "user", "content": f"观察结果：\n{step.observation}"})
        return messages

    @staticmethod
    def _format_assistant_msg(step: ReActStep) -> str:
        parts = [f"思考：{step.thought}"]
        if step.action:
            parts.append(f"行动：{json.dumps(step.action, ensure_ascii=False)}")
        return "\n".join(parts)

# ============================================================
# ReActEngine —— 核心推理引擎
# ============================================================
class ReActEngine:
    """驱动 Think → Act → Observe 循环的引擎"""

    def __init__(
        self,
        model: str = "deepseek-chat",
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_steps: int = MAX_STEPS,
        workspace: str = "",
    ):
        self.model = model
        self.max_steps = max_steps
        self.workspace = workspace
        self.client = OpenAI(
            base_url=base_url or os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
            api_key=api_key or os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY"),
        )
        if not self.client.api_key:
            raise ValueError("❌ 未找到 API Key，请设置 LLM_API_KEY 或 DEEPSEEK_API_KEY")

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------
    def solve(self, question: str) -> str:
        session = ReActSession(
            system_prompt=self._build_system_prompt(),
            user_question=question,
        )

        print(f"\n{'='*60}")
        print(f"🤖 ReAct Agent 启动")
        print(f"📌 问题: {question}")
        print(f"📂 工作区: {self.workspace}")
        print(f"🧠 模型: {self.model}")
        print(f"{'='*60}")

        for step_num in range(1, self.max_steps + 1):
            print(f"\n--- 步骤 {step_num}/{self.max_steps} ---")

            # 1. 调用模型
            raw = self._call_llm(session.to_messages())
            parsed = self._parse_llm_response(raw)

            # 2. 检查是否完成
            if parsed.get("final_answer"):
                step = ReActStep(
                    step_num=step_num,
                    thought=parsed.get("thought", ""),
                    final_answer=parsed["final_answer"],
                )
                session.steps.append(step)
                self._print_step(step)
                return parsed["final_answer"]

            # 3. 执行行动
            action = parsed.get("action")
            if not action:
                # 模型既没给 final_answer 也没给 action —— 容错处理
                thought = parsed.get("thought", raw)
                obs = f"⚠️ 模型未返回有效 action，原始输出：{raw[:500]}"
                step = ReActStep(step_num=step_num, thought=thought, observation=obs)
                session.steps.append(step)
                self._print_step(step)
                continue

            tool_name = action.get("tool", "")
            params = action.get("params", {})
            step = ReActStep(
                step_num=step_num,
                thought=parsed.get("thought", ""),
                action=action,
            )
            self._print_step(step)

            tool_func = ToolRegistry.get(tool_name)
            if not tool_func:
                obs = f"❌ 未知工具: {tool_name}（可用: {', '.join(ToolRegistry.all().keys())}）"
            else:
                try:
                    obs = tool_func(**params)
                except TypeError as e:
                    obs = f"❌ 参数错误: {e}"
                except Exception as e:
                    obs = f"❌ 工具异常: {e}"

            step.observation = obs
            print(f"   🔍 观察: {obs[:200]}{'...' if len(obs) > 200 else ''}")
            session.steps.append(step)

        # 超出最大步数
        last_thought = session.steps[-1].thought if session.steps else ""
        return f"⚠️ 已达到最大步数 {self.max_steps}，强制结束。最后思考：{last_thought[:300]}"

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------
    def _build_system_prompt(self) -> str:
        """渲染系统提示模板"""
        # 生成目录内容摘要
        dir_contents = ""
        try:
            entries = sorted(os.listdir(self.workspace))
            dir_contents = ", ".join(entries[:20])
            if len(entries) > 20:
                dir_contents += f" ... (共 {len(entries)} 项)"
        except Exception:
            dir_contents = "(无法读取)"

        return SYSTEM_PROMPT.format(
            max_steps=self.max_steps,
            tool_descriptions=ToolRegistry.describe_all(),
            os_name=platform.system(),
            work_dir=os.path.abspath(self.workspace),
            dir_contents=dir_contents,
        )

    def _call_llm(self, messages: List[Dict[str, str]]) -> str:
        print("   🧠 正在推理...")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,  # 降低温度以获得更稳定的 JSON 输出
        )
        return response.choices[0].message.content

    @staticmethod
    def _parse_llm_response(raw: str) -> Dict[str, Any]:
        """
        解析 LLM 返回的 JSON。
        容错：支持 ```json ... ``` 代码块包裹，或直接的 JSON 对象。
        """
        # 尝试提取 ```json ... ``` 或 ``` ... ``` 中的 JSON
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        else:
            # 找第一个 { 到最后一个 }
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1:
                return {"thought": raw, "_raw": raw}
            candidate = raw[start:end + 1]

        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 最后兜底：尝试用 ast 做宽松解析
            try:
                return ast.literal_eval(candidate)
            except Exception:
                return {"thought": raw, "_raw": raw}

    @staticmethod
    def _print_step(step: ReActStep):
        print(f"   💭 思考: {step.thought[:150]}{'...' if len(step.thought) > 150 else ''}")
        if step.action:
            t = step.action.get("tool", "?")
            p = step.action.get("params", {})
            print(f"   🔧 行动: {t}({p})")
        if step.final_answer:
            print(f"   ✅ 完成!")

# ============================================================
# 命令行入口
# ============================================================
def main():
    global WORKSPACE

    parser = argparse.ArgumentParser(
        description="ReAct Agent —— 基于 JSON 响应格式的推理式智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="工作目录路径（默认: 当前目录）",
    )
    parser.add_argument(
        "-m", "--model",
        default=os.getenv("LLM_MODEL", "deepseek-chat"),
        help="LLM 模型名称（默认: deepseek-chat）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="LLM API Base URL（默认从环境变量 LLM_BASE_URL 读取）",
    )
    parser.add_argument(
        "-s", "--max-steps",
        type=int,
        default=MAX_STEPS,
        help=f"最大推理步数（默认: {MAX_STEPS}）",
    )
    parser.add_argument(
        "-q", "--question",
        default=None,
        help="直接传入问题（不传则进入交互模式）",
    )
    args = parser.parse_args()

    # 加载环境变量
    load_dotenv()

    # 初始化工作区
    WORKSPACE = os.path.abspath(args.workspace)
    if not os.path.isdir(WORKSPACE):
        print(f"❌ 工作目录不存在: {WORKSPACE}")
        sys.exit(1)

    # 创建引擎
    engine = ReActEngine(
        model=args.model,
        base_url=args.base_url,
        max_steps=args.max_steps,
        workspace=WORKSPACE,
    )

    # 单次执行模式
    if args.question:
        result = engine.solve(args.question)
        print(f"\n{'='*60}")
        print(f"🎯 最终答案:\n{result}")
        print(f"{'='*60}")
        return

    # 交互模式
    print(f"\n🚀 ReAct Agent 已就绪！")
    print(f"   工作区: {WORKSPACE}")
    print(f"   输入问题开始对话，输入 'quit' 或 'exit' 退出\n")

    while True:
        try:
            question = input("🧑 你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见！")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "退出"):
            print("👋 再见！")
            break

        result = engine.solve(question)
        print(f"\n🎯 Agent: {result}\n")

if __name__ == "__main__":
    main()