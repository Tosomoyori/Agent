---
kind: dependency_management
name: 基于 uv + pyproject.toml 的 Python 依赖管理
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .python-version
---

## 1. 使用的系统/方案

本项目使用 **uv**（Rust 实现的 Python 包管理器）作为依赖解析与锁定工具，配合 PEP 621 风格的 `pyproject.toml` 声明依赖，并通过 `uv.lock` 进行可复现的依赖锁定。构建后端为 **hatchling**。

- 包声明：`pyproject.toml` 的 `[project]` / `[project.optional-dependencies]`
- 依赖锁定：`uv.lock`（包含每个包的精确版本、来源 URL、SHA256 hash）
- Python 版本约束：`requires-python = ">=3.11"`（`pyproject.toml`），`.python-version` 指定开发环境为 `3.12`
- 构建系统：`[build-system] requires = ["hatchling"]`, `build-backend = "hatchling.build"`

## 2. 关键文件

- `pyproject.toml` — 项目元数据、运行时依赖、可选依赖、CLI entry point、构建配置、pytest/ruff 配置
- `uv.lock` — 已解析并锁定的完整依赖树（含所有 transitive deps 的精确版本与哈希）
- `.python-version` — 指定本地开发 Python 版本（3.12）

## 3. 架构与约定

### 依赖分类
- **运行时依赖**（`dependencies`）：`openai>=1.40`、`python-dotenv>=1.0`、`pydantic>=2.7`、`pydantic-settings>=2.3`、`fastapi>=0.115`、`uvicorn>=0.30`
- **可选依赖组**（`optional-dependencies.dev`）：`pytest>=8.0`、`pytest-asyncio>=0.23`、`respx>=0.21`、`ruff>=0.6`，通过 extra `dev` 安装
- **CLI 入口**：`agentkit = "agentkit.app.cli:main"`（`[project.scripts]`）

### 锁定策略
`uv.lock` 对每个包记录：
- 精确版本号（如 `fastapi==0.142.2`、`httpx==0.28.1`）
- 来源 registry（`https://pypi.org/simple`）
- sdist/wheel 的 URL 与 SHA256 hash
- 平台/Python 版本的 marker（如 `sys_platform == 'emscripten'`、`python_full_version >= '3.12'`）

这保证了不同机器上解析出的依赖树完全一致。

### 构建产物
`[tool.hatch.build.targets.wheel] packages = ["src/agentkit"]` 指定打包时只包含 `src/agentkit` 目录。

## 4. 约定与约束

- **版本范围使用 `>=` 而非固定版本**：所有依赖在 `pyproject.toml` 中均使用最小版本约束（如 `openai>=1.40`），具体锁定版本由 `uv.lock` 决定；新增依赖时应遵循此模式。
- **可选依赖按用途分组**：仅开发期工具放入 `dev` extra，生产运行不安装这些包。
- **Python 版本一致性**：`requires-python = ">=3.11"` 与 `.python-version = 3.12` 共同约束环境与开发版本。
- **无私有仓库或 vendored 代码**：所有第三方包均来自 PyPI（`source = { registry = "https://pypi.org/simple" }`），未见 `vendor/`、`requirements.txt`、`setup.py`、`poetry.lock`、`pip.conf` 等替代方案。
- **CI/CD 未在此仓库中体现**：未发现 GitHub Actions 或其他 CI 配置文件，依赖更新流程未在仓库内显式规定。