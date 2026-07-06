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

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# P1-4：Agent 记忆向量维度，对齐 OpenAI text-embedding-3-small（1536）。
# 与 knowledge/models.py 的 EMBEDDING_DIM 保持一致，复用同一 embedder。
AGENT_MEMORY_EMBEDDING_DIM = 1536

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
    # P0-2：autonomous loop。schedule 格式 "interval:<seconds>"，schedule_enabled=True
    # 时后台 worker 按 next_run_at 周期唤醒执行；last_run_* 记录最近一次运行状态。
    schedule: Mapped[str | None] = mapped_column(String(128))
    schedule_enabled: Mapped[bool] = mapped_column(default=False)
    last_run_at: Mapped[datetime | None] = mapped_column()
    last_run_status: Mapped[str | None] = mapped_column(String(32))
    last_run_error: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[datetime | None] = mapped_column()
    # P4-2：资源隔离。nullable 兼容旧数据（NULL 视为公共资源，仅 admin 可见）。
    # router 层创建时绑定 current_user.id；service 层 get/list 校验所有权。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_agents_active", "is_active"),
        # P0-2：worker 查询到期 agent 的覆盖索引
        Index("idx_agents_schedule_due", "schedule_enabled", "next_run_at"),
    )


class Workflow(Base):
    """工作流 DAG。nodes + edges 以 JSONB 存储。"""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    edges: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)
    # P4-2：资源隔离。nullable 兼容旧数据（NULL 视为公共资源，仅 admin 可见）。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_workflows_active", "is_active"),)


class AgentMemoryChunk(Base):
    """P1-4：Agent 记忆分块。把每轮 ReAct 的 observation / final_answer 向量化
    存入 pgvector，下次执行时按当前 query 检索 top-k 相关历史片段注入 context。

    与 knowledge ``Chunk`` 解耦——此处无 KB/document FK 绑定，按 ``agent_id``
    隔离命名空间，``session_id`` 标识单次 ``execute_agent`` 调用，``turn`` 标识轮次。
    """

    __tablename__ = "agent_memory_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(AGENT_MEMORY_EMBEDDING_DIM))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # HNSW 向量索引，余弦距离；PG 专属，SQLite 上降级为普通索引（dialect kwargs 被忽略）
        Index(
            "idx_agent_memory_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


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
    max_turns: int = Field(default=10, ge=1, le=50)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # P3-11：自主运维开关（默认关闭，需显式启用）
    self_eval: bool = False
    self_heal: bool = False
    self_eval_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    self_heal_max_retries: int = Field(default=1, ge=0, le=3)
    # P0-2：autonomous loop。schedule="interval:<seconds>"，配合 schedule_enabled 启用。
    schedule: str | None = None
    schedule_enabled: bool = False

    @field_validator("schedule")
    @classmethod
    def _validate_schedule_format(cls, v: str | None) -> str | None:
        """schedule 必须为 ``interval:<seconds>`` 格式（seconds 为正整数）。

        不引入 cron 库以保持零依赖；如需 cron 表达式可后续扩展。

        使用 ``PydanticCustomError`` 而非 ``ValueError`` 以避免 pydantic v2
        将异常对象放入 ``ctx['error']`` 导致 FastAPI JSONResponse 序列化失败。
        """
        if v is None or v == "":
            return None
        if not v.startswith("interval:"):
            raise PydanticCustomError(
                "schedule_format", "schedule 必须为 'interval:<seconds>' 格式"
            )
        try:
            secs = int(v[len("interval:"):])
        except ValueError:
            raise PydanticCustomError(
                "schedule_seconds", "schedule 的 seconds 必须为整数"
            ) from None
        if secs <= 0:
            raise PydanticCustomError(
                "schedule_positive", "schedule 的 seconds 必须为正整数"
            )
        return v


class AgentUpdate(BaseModel):
    """更新 Agent 入参（E1：eval 反馈回写 agent 配置所需）。

    所有字段 Optional——PATCH 语义，仅传入字段被更新（``model_dump(exclude_unset=True)``）。
    字段约束与 ``AgentCreate`` 一致，确保更新值通过同口径校验。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    system_prompt: str | None = None
    model_alias: str | None = None
    tools: list[ToolDef] | None = None
    max_turns: int | None = Field(default=None, ge=1, le=50)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    self_eval: bool | None = None
    self_heal: bool | None = None
    self_eval_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    self_heal_max_retries: int | None = Field(default=None, ge=0, le=3)
    schedule: str | None = None
    schedule_enabled: bool | None = None
    is_active: bool | None = None

    @field_validator("schedule")
    @classmethod
    def _validate_schedule_format(cls, v: str | None) -> str | None:
        """与 ``AgentCreate`` 同口径校验。空字符串视为清除 schedule。"""
        if v is None or v == "":
            return None
        if not v.startswith("interval:"):
            raise PydanticCustomError(
                "schedule_format", "schedule 必须为 'interval:<seconds>' 格式"
            )
        try:
            secs = int(v[len("interval:"):])
        except ValueError:
            raise PydanticCustomError(
                "schedule_seconds", "schedule 的 seconds 必须为整数"
            ) from None
        if secs <= 0:
            raise PydanticCustomError(
                "schedule_positive", "schedule 的 seconds 必须为正整数"
            )
        return v


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
    # P0-2：autonomous loop 字段
    schedule: str | None = None
    schedule_enabled: bool
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_run_error: str | None = None
    next_run_at: datetime | None = None
    # P4-2：资源隔离
    owner_id: uuid.UUID | None = None
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
    # P4-2：资源隔离
    owner_id: uuid.UUID | None = None
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
    # P2-10：执行前 plan + 执行后 reflection。对应功能关闭或 LLM 失败时为 None。
    plan: str | None = None
    reflection: str | None = None


class ExecuteRequest(BaseModel):
    """执行 Agent 请求体。"""

    input: str = Field(min_length=1)
    max_turns: int | None = Field(default=None, ge=1, le=50)
    context: dict[str, Any] = Field(default_factory=dict)
