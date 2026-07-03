"""应用配置 — Pydantic Settings 单例。

环境变量优先级高于默认值。生产部署通过 .env 或 K8s ConfigMap/Secret 注入。
字段命名遵循 `specs/security.spec.md`§2（`JWT_SECRET` / `JWT_EXPIRE_HOURS`）
与 `specs/observability.spec.md`§3（`LOG_LEVEL`）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认占位密钥：仅允许在开发/测试环境使用，生产环境必须通过环境变量显式覆盖。
_INSECURE_DEFAULT_SECRET = "change-me"
# security.spec.md§2.3：JWT_SECRET 最小长度 32 字节。
_MIN_SECRET_BYTES = 32
# 生产环境标识：以下 environment 值触发 fail-fast 校验。
_PROD_ENVS = {"production", "prod", "staging"}


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
        # 允许通过字段名（而非仅 alias）作为 init kwarg 传入，
        # 例如 Settings(environment="production")。否则 kwargs 会被当成 extra
        # 字段并因 extra="ignore" 被静默丢弃，导致 validator 拿不到 production。
        populate_by_name=True,
    )

    # 基础
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    environment: str = Field(default="development", alias="ENVIRONMENT")
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
    jwt_secret: str = Field(default=_INSECURE_DEFAULT_SECRET, alias="JWT_SECRET")
    secret_key: str = Field(default=_INSECURE_DEFAULT_SECRET, alias="SECRET_KEY")
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
        return (
            self.jwt_secret
            if self.jwt_secret != _INSECURE_DEFAULT_SECRET
            else self.secret_key
        )

    @model_validator(mode="after")
    def _validate_secret_key(self) -> Settings:
        """生产环境 fail-fast：拒绝默认占位密钥与过短密钥（security.spec.md§2.3/§9）。

        - 开发/测试环境允许使用占位符 ``change-me`` 以降低本地启动门槛。
        - 生产/staging 环境必须通过环境变量（或 K8s Secret）显式注入
          长度 ≥ 32 字节的强密钥，否则启动直接报错，杜绝弱密钥上生产。
        """
        if self.environment.lower() not in _PROD_ENVS:
            return self
        secret = self.effective_secret_key
        if secret == _INSECURE_DEFAULT_SECRET:
            raise ValueError(
                "JWT_SECRET 必须在生产环境通过环境变量显式设置，"
                "禁止使用默认占位值 'change-me'（security.spec.md§2.3/§9）"
            )
        if len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET 长度不足 {_MIN_SECRET_BYTES} 字节"
                f"（当前 {len(secret.encode('utf-8'))} 字节），"
                "security.spec.md§2.3 要求最小 32 字节"
            )
        return self

    @model_validator(mode="after")
    def _validate_cors(self) -> Settings:
        """CORS 安全校验（security.spec.md§4）。

        禁止 ``allow_origins=["*"]`` 与 ``allow_credentials=True`` 同时出现
        （浏览器规范亦不允许，且是高危误配）。main.py 中 ``allow_credentials=True``
        硬编码，因此 ``cors_origins`` 不得含 ``"*"``——必须显式指定 Origin 列表。
        """
        if "*" in self.cors_origins:
            raise ValueError(
                "CORS_ORIGINS 不得包含 '*'（security.spec.md§4）："
                "allow_credentials=True 与通配 Origin 不可同时使用，"
                "请显式指定允许的 Origin 列表（如 ['https://console.example.com']）"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """返回缓存的单例 Settings。"""
    return Settings()


settings: Settings = get_settings()
