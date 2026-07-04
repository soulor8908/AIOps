"""Agent Orchestrator — ORM + Pydantic schemas。

ORM: Agent, Workflow
Schema: AgentCreate / AgentOut / WorkflowDef / AgentNode / WorkflowOut /
        ExecutionResult / ExecutionTrace / ToolDef / ToolType
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

class ToolType(enum.StrEnum):
    """工具类型。"""

    SEARCH = "search"
    CALCULATOR = "calculator"
    HTTP = "http"
    CODE = "code"
    RAG = "rag"
    CUSTOM = "custom"
    # P3-12：multi-agent A2A。把另一个 Agent 注册为可调用工具，
    # 执行时把 args.input 传给目标 Agent，返回其 final_answer 作为观察。
    AGENT_DELEGATE = "agent_delegate"


# ===================== ORM =====================

class Agent(Base):
    """Agent 定义。ReAct 风格，含 system_prompt / tools / max_turns。"""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    model_alias: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    # P3-11：自主运维。self_eval=True 时执行后用 LLM judge 自评答案质量；
    # self_heal=True 且自评不达标时追加反馈重试，最多 self_heal_max_retries 次。
    self_eval: Mapped[bool] = mapped_column(default=False)
    self_heal: Mapped[bool] = mapped_column(default=False)
    self_eval_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    self_heal_max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_agents_active", "is_active"),)


class Workflow(Base):
    """工作流 DAG。nodes + edges 以 JSONB 存储。"""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    edges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_workflows_active", "is_active"),)


# ===================== Schemas =====================

class ToolDef(BaseModel):
    """工具定义。"""

    name: str
    type: ToolType = ToolType.CUSTOM
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentCreate(BaseModel):
    """创建 Agent 入参。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    system_prompt: str | None = None
    model_alias: str = "default"
    tools: list[ToolDef] = Field(default_factory=list)
    max_turns: int = Field(default=10, ge=1, le=10)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # P3-11：自主运维开关（默认关闭，需显式启用）
    self_eval: bool = False
    self_heal: bool = False
    self_eval_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    self_heal_max_retries: int = Field(default=1, ge=0, le=3)


class AgentOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None = None
    system_prompt: str | None = None
    model_alias: str
    tools: list[dict[str, Any]]
    max_turns: int
    temperature: float
    self_eval: bool
    self_heal: bool
    self_eval_threshold: float
    self_heal_max_retries: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AgentNode(BaseModel):
    """工作流节点定义。"""

    id: str
    agent_id: uuid.UUID | None = None
    name: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    is_entry: bool = False
    is_exit: bool = False


class WorkflowEdge(BaseModel):
    """工作流边。"""

    source: str
    target: str
    condition: str | None = None


class WorkflowDef(BaseModel):
    """创建工作流入参。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    nodes: list[AgentNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)


class WorkflowOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ExecutionTrace(BaseModel):
    """单步执行追踪。"""

    turn: int
    thought: str
    action: str | None = None
    observation: str | None = None
    tokens: int = 0


class ExecutionResult(BaseModel):
    """Agent / Workflow 执行结果。"""

    agent_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    final_answer: str
    traces: list[ExecutionTrace] = Field(default_factory=list)
    total_tokens: int = 0
    success: bool = True
    error: str | None = None
    # P3-11：自主运维结果。self_eval 关闭时为 None。
    eval_score: float | None = None
    eval_reason: str | None = None
    heal_attempts: int = 0


class ExecuteRequest(BaseModel):
    """执行 Agent 请求体。"""

    input: str = Field(min_length=1)
    max_turns: int | None = Field(default=None, ge=1, le=10)
    context: dict[str, Any] = Field(default_factory=dict)
