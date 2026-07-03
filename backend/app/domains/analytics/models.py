"""Conversation Analytics — ORM + Pydantic schemas。

ORM: Conversation, Message
Schema: ConversationOut / MessageOut / DashboardMetrics
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# ===================== ORM =====================

class Conversation(Base):
    """对话。聚合 token 与成本。"""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 跨域引用：以字符串 ForeignKey 声明 DB 级约束（ondelete=SET NULL），
    # 不 import User/Agent ORM，避免跨域 metadata 耦合（migration.spec.md §9 #6）。
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    model_alias: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(255))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_conversations_created", "created_at"),
        # P3：dashboard _active_models 按 model_alias GROUP BY 聚合，缺索引走 Seq Scan。
        Index("idx_conversations_model_alias", "model_alias"),
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at.asc()",
    )


class Message(Base):
    """对话消息。role: user/assistant/system/tool。"""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    model_alias: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("idx_messages_conversation", "conversation_id", "created_at"),
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


# ===================== Schemas =====================

class MessageOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    tokens_in: int
    tokens_out: int
    latency_ms: int | None = None
    model_alias: str | None = None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    model_alias: str | None = None
    title: str | None = None
    total_tokens: int
    total_cost: Decimal
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)


class DashboardMetrics(BaseModel):
    """仪表盘指标。按时间维度聚合。"""

    total_conversations: int = 0
    total_messages: int = 0
    total_tokens: int = 0
    total_cost: Decimal = Field(default=Decimal("0"))
    avg_messages_per_conversation: float = 0.0
    avg_latency_ms: float = 0.0
    active_models: list[dict[str, Any]] = Field(default_factory=list)
    conversations_last_7d: list[dict[str, Any]] = Field(default_factory=list)
