---
kind: configuration_system
name: 基于 pydantic-settings 的进程级配置系统
category: configuration_system
scope:
    - '**'
source_files:
    - src/agentkit/core/config.py
    - .env.example
    - pyproject.toml
    - src/agentkit/app/cli.py
    - src/agentkit/app/api.py
    - src/agentkit/runtime/agent.py
---

## 1. 使用的系统与框架

- **pydantic-settings** (`BaseSettings` + `SettingsConfigDict`) 是唯一的配置加载引擎。
- **python-dotenv** 作为底层依赖，负责从 `.env` 文件读取键值对。
- 通过 `pyproject.toml` 声明依赖：`pydantic>=2.7`、`pydantic-settings>=2.3`、`python-dotenv>=1.0`。
- 没有使用 YAML/TOML/JSON 配置文件；所有运行时参数均通过环境变量或 Python 调用时的关键字覆盖注入。

## 2. 关键文件

- `src/agentkit/core/config.py` — 唯一配置定义与加载入口（`Settings` 类 + `load_settings()`）。
- `.env.example` — 模板，说明应暴露的环境变量名。
- `pyproject.toml` — 声明配置相关依赖及 CLI entry point。
- `src/agentkit/app/cli.py` — CLI 层把 argparse 参数转为 `load_settings(**overrides)` 调用。
- `src/agentkit/app/api.py` — HTTP 服务层同样通过 `load_settings()` 获取配置。
- `src/agentkit/runtime/agent.py` — 运行时也提供默认 `settings=load_settings()`，供直接调用者使用。

## 3. 架构与约定

### 3.1 单一 Settings 模型

`Settings(BaseSettings)` 在 `core/config.py` 中集中声明所有进程级配置项，按语义分组注释：
- LLM 相关：`api_key`、`base_url`、`model`、`temperature`、`max_tokens`、`request_timeout`、`max_retries`、`retry_base_delay`、`retry_max_delay`。
- 运行时：`max_steps`、`workspace`。

每个字段通过 `Field(validation_alias="ENV_NAME", description=...)` 绑定到环境变量，同时保留 Python 属性名。例如 `LLM_API_KEY` → `Settings.api_key`，`AGENT_WORKSPACE` → `Settings.workspace`。

### 3.2 优先级链

文档注释明确写出优先级顺序：**环境变量 > `.env` 文件 > 默认值**。这是 `pydantic-settings` 的默认行为，由 `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")` 启用 `.env` 加载。

### 3.3 别名与兼容

- `api_key` 通过 `AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY")` 同时支持新旧变量名，保持向后兼容。
- `KNOWN_BASE_URLS` 字典集中列出已知的 OpenAI 兼容端点（deepseek/qwen/kimi/glm），`base_url` 默认指向 deepseek。

### 3.4 加载入口

`load_settings(**overrides: object) -> Settings` 是统一入口：
- 过滤掉值为 `None` 的覆盖项（方便命令行参数透传时不覆盖默认值）。
- 其余非 None 值直接传给 `Settings(**clean)`。

CLI 和 API 都通过 `load_settings(model=..., max_steps=..., workspace=...)` 传入局部覆盖，最终得到完整 `Settings` 实例。

### 3.5 校验与错误

`Settings.require_api_key()` 在首次访问 API Key 时检查是否为空，抛出 `ConfigurationError`（来自 `core.errors`），并给出中文提示，指明应复制 `.env.example` 为 `.env` 并填入 `DEEPSEEK_API_KEY`，或直接设置 `LLM_API_KEY`。

`resolved_workspace()` 返回展开用户目录后的绝对路径。

### 3.6 暴露面

`core/__init__.py` 重新导出 `Settings` 和 `load_settings`，使上层模块可以 `from agentkit.core import Settings, load_settings` 或 `from agentkit.core.config import ...` 两种方式导入。

## 4. 约定与约束

- **配置项必须通过 `Field(validation_alias=...)` 显式绑定环境变量**：所有 `Settings` 字段都遵循这一模式，未绑定的字段不会从环境变量自动映射。
- **`.env` 文件编码固定为 UTF-8**：由 `SettingsConfigDict(env_file_encoding="utf-8")` 强制。
- **未知环境变量被忽略**：`extra="ignore"` 确保拼写错误的环境变量不会导致启动失败。
- **API Key 缺失时显式报错**：不是静默失败，而是通过 `require_api_key()` 抛出 `ConfigurationError`，错误消息指导用户如何修复。
- **CLI 参数优先于 `.env`**：CLI 通过 `load_settings(model=args.model, max_steps=args.max_steps, workspace=workspace)` 将解析后的参数作为覆盖注入，覆盖 `.env` 中的同名值。
- **HTTP 服务与 CLI 共享同一配置模型**：两者都调用 `load_settings()`，避免两套配置来源。
- **工作区路径始终可解析为绝对路径**：通过 `resolved_workspace()` 调用 `Path.expanduser().resolve()`。
- **模型名必须是配置项**：代码注释明确指出项目原本硬编码 `deepseek-chat` 的问题，现已迁移到 `Settings.model`（默认 `deepseek-flash`），并通过 `LLM_MODEL` 环境变量覆盖。
- **OpenAI 兼容端点白名单化**：新增 provider 需先登记到 `KNOWN_BASE_URLS`，再配合 `LLM_BASE_URL` 使用。

## 5. 尚未发现的能力

- 未发现 feature flags / 特性开关机制。
- 未发现多环境（dev/staging/prod）配置切换逻辑。
- 未发现 secrets manager 集成（如 AWS Secrets Manager、HashiCorp Vault）。
- 未发现配置热重载能力。
- 未发现配置 schema 验证之外的业务规则校验（除 `require_api_key` 外）。