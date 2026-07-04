"""add eval_samples table for online eval loop

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04

P0-3 Online eval 闭环：新建 ``eval_samples`` 表持久化生产采样样本。

设计要点：
- 与 ``eval_cases``（手工 golden 用例）解耦——采样来自生产路径，``expected``
  可空（``run_online_eval`` 匹配离线 golden 时回填）。
- ``judged`` 标记避免重复评估；``eval_run_id`` 关联消费它的 EvalRun。
- 三个索引：``judged``（查未评估样本）、``sampled_at``（按时间窗口采样）、
  ``agent_id``（按 agent 归因）。

0001 基线已冻结，本迁移用 CREATE TABLE 增量新增，不重建基线。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_samples",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("workflow_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "trigger_source",
            sa.String(length=32),
            nullable=False,
            server_default="http",
        ),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("actual_output", sa.Text(), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("sampled_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("judged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("judge_score", sa.Float(), nullable=True),
        sa.Column("judge_reason", sa.Text(), nullable=True),
        sa.Column("eval_run_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("idx_eval_samples_judged", "eval_samples", ["judged"])
    op.create_index("idx_eval_samples_sampled_at", "eval_samples", ["sampled_at"])
    op.create_index("idx_eval_samples_agent", "eval_samples", ["agent_id"])


def downgrade() -> None:
    op.drop_index("idx_eval_samples_agent", table_name="eval_samples")
    op.drop_index("idx_eval_samples_sampled_at", table_name="eval_samples")
    op.drop_index("idx_eval_samples_judged", table_name="eval_samples")
    op.drop_table("eval_samples")
