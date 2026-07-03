"""Prompt Studio — ORM 模型 + Pydantic schemas。

ORM: Prompt（含 versions 关系）+ PromptVersion
Schema: PromptCreate / PromptUpdate / PromptOut / PromptVersionCreate / PromptVersionOut
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from typing import Self


# ===================== ORM =====================

class PromptVersion(Base):
    """Prompt 版本。每个 Prompt 可有最多 100 个版本。"""

    __tablename__ = "prompt_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), index=True
    )
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list[object]] = mapped_column(JSONB, nullable=False, default=list)
    change_note: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    prompt: Mapped[Prompt] = relationship(
        back_populates="versions", foreign_keys=[prompt_id]
    )


class Prompt(Base):
    """Prompt 主表。current_version_id 指向当前生效版本。"""

    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_versions.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_prompts_active", "is_active"),)

    versions: Mapped[list[PromptVersion]] = relationship(
        back_populates="prompt",
        foreign_keys=[PromptVersion.prompt_id],
        cascade="all, delete-orphan",
        order_by="PromptVersion.version_num.desc()",
    )


# ===================== Schemas =====================

class PromptVersionCreate(BaseModel):
    """新增版本入参。"""

    content: str = Field(min_length=1, max_length=65536)
    variables: list[str] = Field(default_factory=list)
    change_note: str | None = None


class PromptVersionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    prompt_id: uuid.UUID
    version_num: int
    content: str
    variables: list[str]
    change_note: str | None = None
    created_by: str | None = None
    created_at: datetime


class PromptCreate(BaseModel):
    """创建 Prompt 入参（含初始版本内容）。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    content: str = Field(min_length=1, max_length=65536)
    variables: list[str] = Field(default_factory=list)


class PromptUpdate(BaseModel):
    """更新 Prompt 元信息。"""

    name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    is_active: bool | None = None


class PromptOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None = None
    current_version_id: uuid.UUID | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    versions: list[PromptVersionOut] = Field(default_factory=list)

    @classmethod
    def from_orm_with_versions(cls, prompt: Prompt) -> Self:
        return cls.model_validate(prompt)


class DiffResult(BaseModel):
    """版本 diff 结果。"""

    from_version: int
    to_version: int
    added_lines: list[str] = Field(default_factory=list)
    removed_lines: list[str] = Field(default_factory=list)
    unified_diff: list[str] = Field(default_factory=list)
