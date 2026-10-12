---
kind: build_system
name: 基于 Hatchling + uv 的 Python 包构建与测试配置
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .python-version
---

## 1. 使用的系统/工具

- **包构建后端**: `hatchling`（`pyproject.toml` 中 `[build-system]` 指定 `requires = ["hatchling"]`, `build-backend = "hatchling.build"`）。
- **依赖锁定与解析**: `uv`（根目录存在 `uv.lock`，且 `.python-version` 约束 Python 版本），通过 `uv.lock` 锁定所有传递依赖的精确版本与哈希。
- **Python 版本要求**: `requires-python = ">=3.11"`，Ruff target-version 也设为 `py311`。
- **测试框架**: `pytest` + `pytest-asyncio`（`[tool.pytest.ini_options]` 启用 `asyncio_mode = "auto"`）。
- **代码质量**: `ruff`（lint select: `E, F, I, UP, B, SIM`，行宽 100）。
- **可执行入口**: 通过 `[project.scripts]` 暴露 `agentkit = "agentkit.app.cli:main"`。

仓库内**没有** Makefile、Dockerfile、CI 流水线（`.github/workflows` 不存在）、发布脚本或跨编译配置。构建/打包/安装完全由 `hatchling` + `uv` 在本地完成。

## 2. 关键文件

- `pyproject.toml` — 项目元数据、依赖、构建后端、pytest/ruff 配置、CLI 入口。
- `uv.lock` — 确定性依赖锁文件（含每个包的 sdist/wheel URL 与 sha256 哈希）。
- `.python-version` — 声明最低 Python 版本。
- `scripts/capture_stream.py`、`scripts/loadtest.py` — 辅助脚本，非构建脚本。

## 3. 架构与约定

- **源码布局**: `src/agentkit` 单包结构，Hatch 构建目标显式声明 `packages = ["src/agentkit"]`。
- **可选依赖分组**: 通过 `[project.optional-dependencies]` 定义 `dev` 组（pytest、pytest-asyncio、respx、ruff），与运行时依赖解耦。
- **测试隔离**: pytest 默认跳过标记为 `live` 的测试（`addopts = "-m 'not live'"`），需 `-m live` 显式运行；测试通过 `ScriptedModel` 假模型隔离 LLM 不确定性。
- **版本管理**: 版本号硬编码在 `pyproject.toml` 的 `[project] version = "0.1.0"`，无自动化 bump 逻辑。
- **可观测性/评估**: 评估结果以 JSONL/JSON 形式输出到 `eval_reports/`，评估用例位于 `eval_suites/*.jsonl`，由 `src/agentkit/evaluation/` 下的 runner/report 处理，不属于构建流程。

## 4. 约定与约束

- **约束（由配置强制）**:
  - Python 版本必须 `>=3.11`（`pyproject.toml` `requires-python` 与 `uv.lock` `requires-python` 双重声明）。
  - 构建产物只包含 `src/agentkit` 包（`[tool.hatch.build.targets.wheel] packages = ["src/agentkit"]`）。
  - 测试路径固定为 `tests`（`[tool.pytest.ini_options] testpaths = ["tests"]`）。
  - Ruff 规则集固定为 `E, F, I, UP, B, SIM`，行宽 100（`[tool.ruff.lint]`）。
  - 默认不运行 `live` 标记测试（`addopts = "-m 'not live'"`）。
  - CLI 命令名固定为 `agentkit`，指向 `agentkit.app.cli:main`。

- **观察到的约定（描述性）**:
  - 依赖版本使用宽松下限（`>=x.y`）而非固定版本，但通过 `uv.lock` 锁定实际解析结果。
  - 无 CI/CD 流水线、无 Docker 镜像、无 Makefile 任务——开发工作流围绕 `uv run` / `pytest` / `ruff` 直接驱动。
  - 版本变更需要手动编辑 `pyproject.toml` 中的 `version` 字段。