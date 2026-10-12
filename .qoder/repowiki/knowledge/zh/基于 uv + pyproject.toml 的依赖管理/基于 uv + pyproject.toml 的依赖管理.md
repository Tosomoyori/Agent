---
kind: dependency_management
name: 基于 uv + pyproject.toml 的依赖管理
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .python-version
---

## 1. 使用的系统与工具

- **包管理器**: `uv`（由根目录的 `uv.lock` 与 `.python-version` 推断，且 `.ruff_cache/0.16.10` 缓存路径表明项目使用 uv 解析 ruff 版本）。
- **声明式元数据**: `pyproject.toml`（PEP 517/521），构建后端为 `hatchling`（`[build-system] requires = ["hatchling"]`）。
- **锁定文件**: `uv.lock`，记录所有直接依赖与传递依赖的精确版本、来源 URL 与 SHA256 hash，用于可复现安装。
- **Python 版本约束**: `.python-version` 固定为 `3.12`；`pyproject.toml` 中 `requires-python = ">=3.11"`，构建与 lint 目标均为 `py311`。

## 2. 关键文件

- `pyproject.toml`：声明运行时依赖、可选依赖、CLI entry point、构建配置与工具链配置。
- `uv.lock`：完整的依赖解析快照（包含 `agentkit` 自身及 fastapi/openai/pydantic 等全部传递依赖的哈希与 wheel/sdist 地址）。
- `.python-version`：指定开发 Python 解释器版本。

## 3. 架构与约定

### 依赖分类
- **运行时依赖**（`[project].dependencies`）：`openai>=1.40`、`python-dotenv>=1.0`、`pydantic>=2.7`、`pydantic-settings>=2.3`、`fastapi>=0.115`、`uvicorn>=0.30`。这些是 `agentkit` 包发布后仍需安装的库。
- **可选依赖组**（`[project.optional-dependencies]`）：仅 `dev` 组，包含 `pytest>=8.0`、`pytest-asyncio>=0.23`、`respx>=0.21`、`ruff>=0.6`，通过 `pip install .[dev]` 或 `uv pip install -e .[dev]` 安装。
- **构建依赖**：`hatchling`，仅在构建 wheel 时使用，不进入运行时依赖。

### 入口点
- CLI 命令 `agentkit` 映射到 `agentkit.app.cli:main`（`[project.scripts]`）。

### 平台与标记
- `uv.lock` 顶部声明 `resolution-markers`，对 `python_full_version >= '3.12' and sys_platform == 'emscripten'` 与非 emscripten 平台分别解析，体现对 Emscripten 平台的差异化依赖处理。
- 部分传递依赖（如 `httpx2-jsfetch`）通过 `sys_platform == 'emscripten'` marker 条件引入。

### 可复现性
- 所有第三方包在 `uv.lock` 中以 `[[package]]` 条目记录，包含 `source.registry`（统一指向 `https://pypi.org/simple`）、`sdist` 与 `wheels` 的完整 URL 及 `sha256` hash，确保离线/CI 环境可精确还原。

## 4. 约定与约束

- **版本约束风格**：运行时依赖采用 `>=X.Y` 的宽松下限约束（例如 `openai>=1.40`、`fastapi>=0.115`、`pydantic>=2.7`），具体被锁定的精确版本由 `uv.lock` 决定；可选 dev 依赖同样使用 `>=` 下限。
- **Python 版本下限**：`requires-python = ">=3.11"` 与 `target-version = "py311"`（ruff）共同约束最低运行与代码检查版本。
- **无私有仓库/Vendoring**：`uv.lock` 中所有包的 `source.registry` 均为 `https://pypi.org/simple`，未发现自定义 index、`GOVPCATE` 等价物或 vendored 第三方源码目录；依赖全部来自 PyPI。
- **无 `requirements.txt` / `setup.py`**：项目完全迁移至 PEP 517 风格的 `pyproject.toml` + `uv.lock`，不存在旧式依赖清单。
- **测试隔离**：通过 pytest marker `live` 将需要真实 LLM 调用的测试默认跳过（`addopts = "-m 'not live'"`），需显式 `-m live` 运行，这属于测试依赖的使用约定而非依赖声明本身。
- **构建产物范围**：`[tool.hatch.build.targets.wheel] packages = ["src/agentkit"]`，仅打包 `src/agentkit` 子包，排除 tests、scripts、docs 等非运行时内容。