# 🤖 ReAct Agent — 基于大模型的自主推理智能体

一个轻量级、可扩展的 **ReAct（Reasoning + Acting）范式** AI Agent 实现。智能体通过“思考 → 行动 → 观察”的循环自主完成复杂任务，支持文件操作、命令执行等工具，内置安全防护机制。适合用于自动化运维、文件管理、信息检索等场景。

---

## ✨ 特性

- **ReAct 推理循环**：模型自主决策每一步动作，动态调用工具，根据观察结果持续推理直至问题解决。
- **工具注册机制**：基于装饰器的工具注册中心，方便扩展自定义工具。
- **内置常用工具**：
  - `read_file` / `write_file`：读写文件（自动限制工作区路径）
  - `list_directory`：列出目录内容
  - `search_in_file`：在文件中搜索关键词
  - `run_command`：执行终端命令（带危险拦截和用户确认）
- **安全防护**：
  - 路径白名单：所有文件操作限制在指定工作区内，防止路径遍历攻击。
  - 危险命令拦截：正则匹配 `rm -rf`、`shutdown`、`format` 等破坏性命令。
  - 用户确认：执行终端命令前需要手动确认。
  - 超时保护：命令执行默认 30 秒超时，防止卡死。
- **灵活的配置**：支持自定义模型、API 地址、最大推理步数等。
- **交互式/单次执行**：既支持命令行单次问答，也支持交互式对话模式。

---

## 🧠 工作原理

Agent 遵循 **ReAct** 范式，每次迭代输出一个 JSON 结构，包含：

- `thought`：当前推理过程
- `action`（行动模式）：要调用的工具名称和参数
- `final_answer`（完成模式）：直接给出最终答案

系统提示通过 `prompt_template.py` 动态生成，包含工具描述、环境信息等，引导模型输出合法 JSON。

---

## 🚀 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/sdnejsn/react-agent.git
cd react-agent
```

### 2. 安装依赖

建议使用 Python 3.9+，并创建虚拟环境：

```bash
pip install openai python-dotenv
```

### 3. 配置 API Key

在项目根目录创建 `.env` 文件（或设置环境变量）：

```env
DEEPSEEK_API_KEY=______________________
```

> 当前仅支持 OpenAI 兼容 API（如 DeepSeek、GPT 等），通过 `OpenAI` 客户端调用。

### 4. 运行

**交互模式**（持续对话）：

```bash
python agent.py /path/to/workspace
```

输入问题后，Agent 会逐步执行并返回结果。输入 `quit` 或 `exit` 退出。

**单次执行模式**：

```bash
python agent.py /path/to/workspace -q "帮我查看当前目录下所有Python文件的行数"
```

可选参数：

- `-m, --model`：指定模型名称（默认 `deepseek-chat`）
- `--base-url`：指定 API Base URL
- `-s, --max-steps`：最大推理步数（默认 15）

---

## 🔧 工具列表

| 工具名 | 参数 | 描述 |
|--------|------|------|
| `read_file` | `file_path` | 读取文件完整内容（仅限工作区内） |
| `write_file` | `file_path`, `content` | 写入内容到文件（覆盖，自动创建父目录） |
| `list_directory` | `path`（可选，默认 `.`） | 列出目录下的文件和子目录 |
| `search_in_file` | `file_path`, `keyword` | 在文件中搜索关键词，返回匹配行及行号 |
| `run_command` | `command` | 执行终端命令（需用户确认，内置危险拦截） |

### 扩展自定义工具

使用 `@ToolRegistry.register` 装饰器即可：

```python
from tool_registry import ToolRegistry

@ToolRegistry.register("my_tool")
def my_tool(param: str) -> str:
    """工具描述"""
    return f"处理结果: {param}"
```

---

## 📁 项目结构

```
.
├── agent.py              # 核心引擎、工具注册、命令行入口
├── prompt_template.py    # 系统提示模板
├── .env                  # 环境变量（需自行创建）
└── README.md
```

---

## 🛡️ 安全设计

- **路径限制**：所有文件操作必须在工作目录内，相对路径会被解析为工作目录下的绝对路径，越界访问会被拒绝。
- **危险命令黑名单**：`rm -rf`、`shutdown`、`format`、`del /f` 等模式被正则匹配拦截。
- **命令执行确认**：任何 `run_command` 调用都会在终端提示用户确认，避免误操作。
- **超时保护**：命令执行默认 30 秒超时，防止恶意或长时间运行命令。

---

## 📊 使用示例

**交互会话示例**（假设工作区包含 `test.py`）：

```
🧑 你: 读取 test.py 并告诉我它有多少行

--- 步骤 1/15 ---
   💭 思考: 需要读取 test.py 文件的内容，然后统计行数。
   🔧 行动: read_file({'file_path': 'test.py'})
   🔍 观察: 📄 /path/to/work/test.py（42 行）
def hello():
    print("Hello")
...

--- 步骤 2/15 ---
   💭 思考: 我已获得文件内容，统计行数为 42，可以直接回答。
   ✅ 完成!

🎯 Agent: test.py 共有 42 行。
```

---

## 🔮 未来扩展方向

- **多轮对话记忆**：在当前会话中保留历史问答上下文，支持连续对话。
- **自动检索策略选择**：根据问题类型动态决定是否调用搜索引擎或其他知识源。
- **更多工具**：如 HTTP 请求、数据库查询、图像处理等。
- **流式输出**：支持思考过程流式显示，提升交互体验。

---
