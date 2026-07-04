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
from sqlalchemy import Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# ===================== 枚举 =====================

class JudgeType(enum.StrEnum):
    """判官类型。"""

    EXACT = "exact"
    CONTAINS = "contains"
    LLM = "llm"
    SEMANTIC = "semantic"


class EvalStatus(enum.StrEnum):
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
    judge_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JudgeType.EXACT.value
    )
    expected: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("idx_eval_rules_judge_type", "judge_type"),)


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

    __table_args__ = (Index("idx_eval_judges_judge_type", "judge_type"),)


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

    __table_args__ = (Index("idx_eval_cases_name", "name"),)


class EvalRun(Base):
    """评估运行记录。UUID 主键，支持分布式追踪。"""

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    judge_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JudgeType.EXACT.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EvalStatus.PENDING.value
    )
    results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float | None] = mapped_column(Float)
    # P1-6：基线 score（上次成功 run 的 score）+ regression 标记。
    # 若当前 score 低于 baseline 超过 _REGRESSION_THRESHOLD（默认 0.05），
    # 标记 is_regression=True，供回归检测告警。
    baseline_score: Mapped[float | None] = mapped_column(Float)
    is_regression: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_eval_runs_status", "status"),
        Index("idx_eval_runs_created", "created_at"),
    )


class EvalSample(Base):
    """生产采样样本（P0-3 online eval 闭环）。

    记录真实请求/响应文本对，供后续 LLM judge 评估。与 ``EvalCase``（手工
    golden 用例）的区别：``EvalSample`` 来自生产路径自动采样，``expected`` 可空
    （匹配离线 golden 时填充）。``judged`` 标记是否已被某次 online eval 消费。
    """

    __tablename__ = "eval_samples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # agent_id / workflow_id 可空：scheduled autonomous run 无 agent_id 上下文，
    # 或未来从 chat 域采样时无 agent 归属。
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 触发来源：http / scheduled / delegate / workflow，便于按来源过滤采样。
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="http")
    input: Mapped[str] = mapped_column(Text, nullable=False)
    actual_output: Mapped[str] = mapped_column(Text, nullable=False)
    # expected 由 run_online_eval 匹配离线 golden 时回填（按 input 或 metadata.tag）。
    expected_output: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    sampled_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # judge 结果回写字段：评估完成后填充，避免重复评估。
    judged: Mapped[bool] = mapped_column(default=False)
    judge_score: Mapped[float | None] = mapped_column(Float)
    judge_reason: Mapped[str | None] = mapped_column(Text)
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        Index("idx_eval_samples_judged", "judged"),
        Index("idx_eval_samples_sampled_at", "sampled_at"),
        Index("idx_eval_samples_agent", "agent_id"),
    )


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
    baseline_score: float | None = None
    is_regression: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EvalSampleCreate(BaseModel):
    """录入生产采样样本（P0-3）。

    供 ``execute_agent`` 自动采样钩子或 ``POST /evals/samples`` 手动录入使用。
    """

    agent_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    trigger_source: str = "http"
    input: str = Field(min_length=1)
    actual_output: str = Field(min_length=1)
    expected_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalSampleOut(BaseModel):
    """采样样本出参。"""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    agent_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    trigger_source: str
    input: str
    actual_output: str
    expected_output: str | None = None
    # validation_alias 从 ORM 的 metadata_ 属性读取（避免与 SQLAlchemy 的
    # metadata 属性冲突——后者是 MetaData 对象，非业务字典）。
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    sampled_at: datetime
    judged: bool
    judge_score: float | None = None
    judge_reason: str | None = None
    eval_run_id: uuid.UUID | None = None


class OnlineEvalRequest(BaseModel):
    """触发 online eval 闭环请求。

    - ``sample_ids``：待评估的样本 ID 列表（空则评估所有未 judged 样本）。
    - ``golden_run_name``：离线 golden EvalRun 的 name，用于匹配 expected 并
      复用基线回归检测（``_fetch_baseline_score`` 按 name 取基线）。
    - ``judge_type``：判官类型，默认 LLM（生产场景多为开放式输出，exact/contains
      不适用）。
    - ``run_name``：本次 online eval 产出的 EvalRun 的 name，默认与
      ``golden_run_name`` 相同以复用基线机制。
    """

    sample_ids: list[uuid.UUID] = Field(default_factory=list)
    golden_run_name: str = Field(min_length=1, max_length=128)
    judge_type: JudgeType = JudgeType.LLM
    run_name: str | None = None
