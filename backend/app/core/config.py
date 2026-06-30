"""应用配置 — Pydantic Settings 单例。

环境变量优先级高于默认值。生产部署通过 .env 或 K8s ConfigMap 注入。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 数据库 / 缓存
    database_url: str = Field(
        default="postgresql+asyncpg://aiops:aiops@localhost:5432/aiops",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # 安全
    secret_key: str = Field(default="change-me", alias="SECRET_KEY")
    access_token_expire_minutes: int = Field(default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES")
    cors_origins: list[str] = Field(default_factory=lambda: ["*"], alias="CORS_ORIGINS")

    # LLM 默认
    default_llm_provider: str = Field(default="openai", alias="DEFAULT_LLM_PROVIDER")
    default_llm_model: str = Field(default="gpt-4o-mini", alias="DEFAULT_LLM_MODEL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")


@lru_cache
def get_settings() -> Settings:
    """返回缓存的单例 Settings。"""
    return Settings()


settings: Settings = get_settings()
