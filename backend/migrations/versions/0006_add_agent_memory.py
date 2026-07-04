"""add agent_memory_chunks table for P1-4 agent memory layer

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-04

P1-4 Agent memory layer：新建 ``agent_memory_chunks`` 表持久化每轮 ReAct 的
observation / final_answer 向量化记忆，下次执行时按当前 query 检索 top-k
相关历史片段注入 context，替换原 LLM 摘要压缩（``_compress_context``）。

设计要点：
- 与 knowledge ``chunks`` 解耦——无 KB/document FK，按 ``agent_id`` 隔离命名空间。
- ``session_id`` 标识单次 ``execute_agent`` 调用，``turn`` 标识轮次，便于按会话/轮次归因。
- HNSW 向量索引（余弦距离），PG 专属；SQLite 测试环境由 ORM ``create_all`` 建表，
  pgvector 算子不可用时 ``search_memory`` 返回空列表（降级为无记忆注入）。
- ``metadata`` 列在 ORM 中映射为 ``metadata_`` 属性，规避 SQLAlchemy ORM ``metadata``
  属性冲突（与 ``EvalSample`` 同模式）。

0001 基线已冻结，本迁移用 CREATE TABLE 增量新增，不重建基线。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_memory_chunks",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_agent_memory_chunks_agent_id", "agent_memory_chunks", ["agent_id"]
    )
    op.create_index(
        "ix_agent_memory_chunks_session_id", "agent_memory_chunks", ["session_id"]
    )
    # HNSW 向量索引（PG 专属）。SQLite 上 dialect kwargs 被忽略，降级为普通索引。
    op.create_index(
        "idx_agent_memory_embedding",
        "agent_memory_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_agent_memory_embedding", table_name="agent_memory_chunks")
    op.drop_index(
        "ix_agent_memory_chunks_session_id", table_name="agent_memory_chunks"
    )
    op.drop_index(
        "ix_agent_memory_chunks_agent_id", table_name="agent_memory_chunks"
    )
    op.drop_table("agent_memory_chunks")
