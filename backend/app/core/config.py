"""应用配置 — Pydantic Settings 单例。

环境变量优先级高于默认值。生产部署通过 .env 或 K8s ConfigMap/Secret 注入。
字段命名遵循 `specs/security.spec.md`§2（`JWT_SECRET` / `JWT_EXPIRE_HOURS`）
与 `specs/observability.spec.md`§3（`LOG_LEVEL`）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_cors_origins() -> list[str]:
    """开发环境默认放宽到本地前端；生产须通过环境变量显式覆盖。

    `security.spec.md`§4 禁止生产环境 ``allow_origins=["*"]`` + ``allow_credentials=True``。
    """
    return ["http://localhost:5173", "http://localhost:3000"]


class Settings(BaseSettings):
    """全局配置，所有字段从环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 基础
    app_version: str = Field(default="0.1.0-alpha", alias="APP_VERSION")
    debug: bool = Field(default=True, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 数据库 / 缓存
    database_url: str = Field(
        default="postgresql+asyncpg://aiops:aiops@localhost:5432/aiops",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # 安全 — JWT
    # `JWT_SECRET` 为真源（`security.spec.md`§2.3），`SECRET_KEY` 作为兼容别名。
    jwt_secret: str = Field(default="change-me", alias="JWT_SECRET")
    secret_key: str = Field(default="change-me", alias="SECRET_KEY")
    jwt_expire_hours: int = Field(default=24, alias="JWT_EXPIRE_HOURS")
    # 兼容旧字段：若显式设置则覆盖 hours 计算；默认 None 表示由 hours 派生。
    access_token_expire_minutes: int | None = Field(
        default=None, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")
    cors_origins: list[str] = Field(default_factory=_default_cors_origins, alias="CORS_ORIGINS")

    # LLM 默认
    default_llm_provider: str = Field(default="openai", alias="DEFAULT_LLM_PROVIDER")
    default_llm_model: str = Field(default="gpt-4o-mini", alias="DEFAULT_LLM_MODEL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    @property
    def access_token_expire_seconds(self) -> int:
        """access token 实际过期秒数。

        优先使用 `ACCESS_TOKEN_EXPIRE_MINUTES`（向后兼容），否则由
        `JWT_EXPIRE_HOURS` 派生（默认 24h，遵循 `security.spec.md`§2.2）。
        """
        if self.access_token_expire_minutes is not None:
            return self.access_token_expire_minutes * 60
        return self.jwt_expire_hours * 3600

    @property
    def refresh_token_expire_seconds(self) -> int:
        """refresh token 过期秒数（默认 7d）。"""
        return self.refresh_token_expire_days * 86400

    @property
    def effective_secret_key(self) -> str:
        """返回实际使用的 JWT 签名密钥（JWT_SECRET 优先于 SECRET_KEY）。"""
        # 若 JWT_SECRET 被显式覆盖（非默认值）则用它，否则回退到 SECRET_KEY
        return self.jwt_secret if self.jwt_secret != "change-me" else self.secret_key


@lru_cache
def get_settings() -> Settings:
    """返回缓存的单例 Settings。"""
    return Settings()


settings: Settings = get_settings()
