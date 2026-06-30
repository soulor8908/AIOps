"""Eval Suite — ORM + Pydantic schemas。

ORM: EvalRule, EvalJudge, EvalCase, EvalRun（UUID 主键）
Schema: EvalRunCreate / EvalRunOut
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


# ===================== 枚举 =====================

class JudgeType(str, enum.Enum):
    """判官类型。"""

    EXACT = "exact"
    CONTAINS = "contains"
    LLM = "llm"
    SEMANTIC = "semantic"


class EvalStatus(str, enum.Enum):
    """评估状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


# ===================== ORM =====================

class EvalRule(Base):
    """评估规则定义（单条断言）。"""

    __tablename__ = "eval_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    judge_type: Mapped[str] = mapped_column(String(32), nullable=False, default=JudgeType.EXACT.value)
    expected: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalJudge(Base):
    """判官配置（LLM-as-judge 的模型与 prompt）。"""

    __tablename__ = "eval_judges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    judge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalCase(Base):
    """评估用例。input + expected + 可选 metadata。"""

    __tablename__ = "eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(128))
    input: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvalRun(Base):
    """评估运行记录。UUID 主键，支持分布式追踪。"""

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    judge_type: Mapped[str] = mapped_column(String(32), nullable=False, default=JudgeType.EXACT.value)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=EvalStatus.PENDING.value)
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


# ===================== Schemas =====================

class EvalCaseInput(BaseModel):
    """评估用例入参。"""

    name: str | None = None
    input: str = Field(min_length=1)
    expected: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalRuleInput(BaseModel):
    """评估规则入参。"""

    name: str
    judge_type: JudgeType = JudgeType.EXACT
    expected: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class EvalRunCreate(BaseModel):
    """创建 eval 入参。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    rules: list[EvalRuleInput] = Field(default_factory=list)
    # 非空校验由 service.create_eval 强制（抛 AppError ValidationError），
    # 与 codebase 其余领域（prompts/knowledge）的 service 层校验风格一致。
    cases: list[EvalCaseInput] = Field(default_factory=list)
    judge_type: JudgeType = JudgeType.EXACT


class CaseResult(BaseModel):
    """单条用例评估结果。"""

    case_name: str | None = None
    input: str
    expected: str | None = None
    actual: str | None = None
    passed: bool
    score: float = 0.0
    reason: str | None = None


class EvalRunOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None = None
    rules: list[dict[str, Any]]
    cases: list[dict[str, Any]]
    judge_type: str
    status: str
    results: list[dict[str, Any]] | None = None
    pass_count: int
    fail_count: int
    score: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
