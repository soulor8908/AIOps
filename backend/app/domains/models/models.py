"""Model Router — ORM + Pydantic schemas。

ORM: ModelConfig
Schema: ModelProvider / ModelConfigCreate / ModelConfigOut /
        ChatRequest / ChatMessage / ChatResponse
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Float, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


# ===================== 枚举 =====================

class ModelProvider(str, enum.Enum):
    """模型供应商。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    AZURE_OPENAI = "azure_openai"
    CUSTOM = "custom"


class RoutingStrategy(str, enum.Enum):
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
    cost_per_1k_input: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    cost_per_1k_output: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# ===================== Schemas =====================

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
