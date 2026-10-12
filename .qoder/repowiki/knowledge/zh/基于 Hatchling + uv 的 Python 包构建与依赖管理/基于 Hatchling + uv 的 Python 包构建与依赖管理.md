---
kind: build_system
name: 基于 Hatchling + uv 的 Python 包构建与依赖管理
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .python-version
---

## 1. 使用的系统/方法

- **包构建后端**：Hatchling（`pyproject.toml` 中 `[build-system]` 指定 `hatchling.build`）。
- **依赖解析与虚拟环境**：uv（根目录存在 `uv.lock`，配合 `.python-version` 锁定 Python 版本），项目未使用 pip/pip-tools。
- **测试框架**：pytest（含 `pytest-asyncio`、`respx`），通过 `tool.pytest.ini_options` 配置。
- **代码质量**：Ruff（`tool.ruff` 配置），启用 E/F/I/UP/B/SIM 规则集，行宽 100，目标 Python 3.11。
- **无 CI/Dockerfile/Makefile**：仓库根不存在 `.github/workflows`、`Dockerfile`、`Makefile` 等文件，发布与持续集成不在本仓库内维护。

## 2. 关键文件

- `pyproject.toml`：唯一的项目元数据、依赖、构建后端、pytest/Ruff 配置入口。
- `uv.lock`：由 uv 生成的锁文件，冻结所有依赖精确版本。
- `.python-version`：声明项目要求的 Python 版本（供 uv/pyenv 使用）。
- `.env.example`：运行时环境变量模板（非构建产物）。
- `src/agentkit/app/cli.py`：通过 `[project.scripts]` 暴露 `agentkit` CLI 入口点。

## 3. 架构与约定

- **源码布局**：采用 PEP 420 风格的 `src/` 布局，`packages = ["src/agentkit"]` 明确只打包该目录。
- **Python 版本约束**：`requires-python = ">=3.11"`，Ruff target-version 同为 py311，二者保持一致。
- **可选依赖分组**：`dev` 组包含 pytest、pytest-asyncio、respx、ruff；运行时仅依赖 openai、python-dotenv、pydantic、fastapi、uvicorn。
- **CLI 安装方式**：通过 `agentkit = "agentkit.app.cli:main"` 脚本入口，安装后可直接调用 `agentkit` 命令。
- **测试默认行为**：pytest 默认跳过标记为 `live` 的测试（`addopts = "-m 'not live'"`），需显式 `-m live` 才运行真实 LLM 调用用例。
- **异步测试模式**：`asyncio_mode = "auto"`，无需在每个 test 函数上加装饰器即可使用 async def。

## 4. 约定与约束

- **构建后端固定为 hatchling**：`[build-system] requires = ["hatchling"]`，禁止切换其他后端（如 setuptools）。来源：`pyproject.toml`。
- **包名与入口点固定**：包名为 `agentkit`，CLI 入口点固定为 `agentkit.app.cli:main`。来源：`pyproject.toml` 的 `[project]` 与 `[project.scripts]`。
- **Python 最低版本 ≥3.11**：由 `requires-python` 强制，任何低于 3.11 的环境在安装/构建时会被拒绝。来源：`pyproject.toml`。
- **依赖版本锁定**：所有第三方依赖版本由 `uv.lock` 锁定，开发时应通过 `uv sync` 而非 `pip install` 同步。来源：`uv.lock` 的存在及 `pyproject.toml` 的依赖声明。
- **测试路径与异步模式**：测试必须放在 `tests/` 目录下，且 async 测试函数可自动被 pytest-asyncio 识别。来源：`tool.pytest.ini_options`。
- **Live 测试默认禁用**：带 `@pytest.mark.live` 的用例不会被常规 `pytest` 触发，避免意外调用外部 LLM。来源：`tool.pytest.ini_options.addopts`。
- **Ruff 规则集固定**：仅启用 E/F/I/UP/B/SIM 五类规则，未启用更多规则集（如 C90、PL、ARG 等）。来源：`tool.ruff.lint.select`。
- **无跨平台构建/容器化脚本**：仓库未提供 Makefile、Dockerfile、CI YAML，因此不存在跨编译或镜像构建流程。