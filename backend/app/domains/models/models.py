"""Model Router — ORM + Pydantic schemas。

ORM: ModelConfig
Schema: ModelProvider / ModelConfigCreate / ModelConfigOut /
        ChatRequest / ChatMessage / ChatResponse
"""

from __future__ import annotations

import enum
import ipaddress
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.exceptions import ValidationError

# ===================== 枚举 =====================

class ModelProvider(enum.StrEnum):
    """模型供应商。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    AZURE_OPENAI = "azure_openai"
    CUSTOM = "custom"


class RoutingStrategy(enum.StrEnum):
    """路由策略。"""

    DIRECT = "direct"
    ROUND_ROBIN = "round_robin"
    LEAST_COST = "least_cost"
    LATENCY = "latency"


# ===================== ORM =====================

class ModelConfig(Base):
    """模型配置。alias 唯一，priority 越小越优先（fallback 顺序）。"""

    __tablename__ = "model_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alias: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_base: Mapped[str | None] = mapped_column(Text)
    api_key_env: Mapped[str | None] = mapped_column(String(64))
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    cost_per_1k_input: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    cost_per_1k_output: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # P4-2：资源隔离。nullable 兼容旧数据（NULL 视为公共资源，仅 admin 可见）。
    # ModelConfig 通常由 admin 维护，但绑定 owner_id 以支持未来「用户自带 model key」场景。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_models_active_priority", "is_active", "priority"),
    )


# ===================== Schemas =====================


def _validate_api_base(value: str | None) -> str | None:
    """SSRF 防护：拒绝指向内网/环回/元数据地址的 api_base。

    security.spec.md — LLM/embedder 出站地址不得指向私网段、环回、link-local
    或云元数据端点（169.254.169.254），否则普通用户可借 chat/RAG 探测内网。
    None 表示使用 provider 默认地址（如 OpenAI 官方），放行。
    """
    if value is None or value == "":
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(
            f"api_base 必须使用 http/https 协议（当前: {parsed.scheme or '空'}）"
        )
    host = parsed.hostname
    if not host:
        raise ValidationError(f"api_base 缺少 host: {value}")
    # 拒绝 localhost 主机名
    if host.lower() in ("localhost",):
        raise ValidationError("api_base 禁止指向 localhost")
    # 尝试解析为 IP 地址（IPv4/IPv6），拒绝私网/环回/link-local/保留段
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 非 IP 字面量（域名），放行——DNS 解析后的 IP 校验应在 httpx transport 层
        # 完成（更彻底），此处先拦截明显的 IP 字面量 SSRF
        return value
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValidationError(
            f"api_base 禁止指向内网/环回/link-local 地址: {host}"
        )
    return value


class ModelConfigCreate(BaseModel):
    """创建模型配置入参。"""

    alias: str = Field(min_length=1, max_length=64)
    provider: ModelProvider = ModelProvider.OPENAI
    model_name: str = Field(min_length=1, max_length=128)
    api_base: str | None = None
    api_key_env: str | None = None
    max_tokens: int = Field(default=4096, ge=1, le=200000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    cost_per_1k_input: Decimal = Field(default=Decimal("0"))
    cost_per_1k_output: Decimal = Field(default=Decimal("0"))
    priority: int = Field(default=100, ge=0, le=1000)
    is_active: bool = True

    @field_validator("api_base")
    @classmethod
    def _check_api_base(cls, v: str | None) -> str | None:
        return _validate_api_base(v)


class ModelConfigUpdate(BaseModel):
    """更新模型配置入参。"""

    model_name: str | None = Field(default=None, max_length=128)
    api_base: str | None = None
    api_key_env: str | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    cost_per_1k_input: Decimal | None = None
    cost_per_1k_output: Decimal | None = None
    is_active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)

    @field_validator("api_base")
    @classmethod
    def _check_api_base(cls, v: str | None) -> str | None:
        return _validate_api_base(v)


class ModelConfigOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    alias: str
    provider: str
    model_name: str
    api_base: str | None = None
    api_key_env: str | None = None
    max_tokens: int
    temperature: float
    cost_per_1k_input: Decimal
    cost_per_1k_output: Decimal
    is_active: bool
    priority: int
    # P4-2：资源隔离
    owner_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessage(BaseModel):
    """对话消息。"""

    role: str = Field(min_length=1, max_length=16)
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """聊天请求。"""

    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    strategy: RoutingStrategy = RoutingStrategy.DIRECT


class ChatResponse(BaseModel):
    """聊天响应。"""

    content: str
    model: str
    alias: str
    usage: dict[str, Any] = Field(default_factory=dict)
    cost: Decimal = Field(default=Decimal("0"))
    fallback_used: bool = False
