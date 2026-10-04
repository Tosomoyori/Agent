"""运行配置。

环境变量优先于 ``.env`` 文件，``.env`` 优先于默认值——这是 pydantic-settings 的
默认优先级，也是容器化部署时想要的行为。

模型名**必须是配置项**：项目原本硬编码 ``deepseek-chat``，而该模型已于 2026 年下线，
`GET /models` 现在只返回 ``deepseek-flash`` 和 ``deepseek-v4-pro``。硬编码模型名
等于给自己埋一颗定时炸弹。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 已知的 OpenAI 兼容端点。DeepSeek 之外的多为占位，用前请核对官方文档。
KNOWN_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}


class Settings(BaseSettings):
    """进程级配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ------------------------------------------------------------ LLM

    #: LLM_API_KEY 优先，回退到 DEEPSEEK_API_KEY（沿用旧项目的变量名）。
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY"),
        description="LLM API Key。",
    )
    base_url: str = Field(
        default=KNOWN_BASE_URLS["deepseek"],
        validation_alias="LLM_BASE_URL",
        description="OpenAI 兼容端点。",
    )
    model: str = Field(
        default="deepseek-flash",
        validation_alias="LLM_MODEL",
        description="模型 id。账号下当前可用：deepseek-flash / deepseek-v4-pro。",
    )

    # ------------------------------------------------------------ 生成参数

    temperature: float = Field(default=0.0, validation_alias="LLM_TEMPERATURE")
    max_tokens: int | None = Field(
        default=None,
        validation_alias="LLM_MAX_TOKENS",
        description="单次回复的输出上限。None 表示用模型默认值。",
    )
    request_timeout: float = Field(default=120.0, validation_alias="LLM_TIMEOUT")
    #: 总尝试次数（含首次）。1 表示不重试。
    max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")
    retry_base_delay: float = Field(default=0.5, validation_alias="LLM_RETRY_BASE_DELAY")
    retry_max_delay: float = Field(default=30.0, validation_alias="LLM_RETRY_MAX_DELAY")

    # ------------------------------------------------------------ 运行时

    max_steps: int = Field(default=15, validation_alias="AGENT_MAX_STEPS")
    workspace: Path = Field(
        default_factory=Path.cwd,
        validation_alias="AGENT_WORKSPACE",
        description="工具可以读写的工作区根目录。",
    )

    def require_api_key(self) -> str:
        """取 API Key，缺失时报一个指得清楚的错。"""
        from .errors import ConfigurationError

        if not self.api_key:
            raise ConfigurationError(
                "未配置 API Key。请复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY，"
                "或设置环境变量 LLM_API_KEY。"
            )
        return self.api_key

    def resolved_workspace(self) -> Path:
        """工作区的绝对路径。"""
        return Path(self.workspace).expanduser().resolve()


def load_settings(**overrides: object) -> Settings:
    """加载配置。``overrides`` 里值为 ``None`` 的项会被忽略，方便命令行参数覆盖。"""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)  # type: ignore[arg-type]
