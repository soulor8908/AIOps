"""add agent schedule columns for autonomous loop

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04

P0-2 Agent autonomous loop：为 agents 表新增 schedule 相关列，支持后台 worker
周期唤醒 Agent 执行。新增列均可空/有默认值，向后兼容已有数据。

- ``schedule``: "interval:<seconds>" 格式字符串，None 表示无调度
- ``schedule_enabled``: 调度开关，默认 False
- ``last_run_at`` / ``last_run_status`` / ``last_run_error``: 最近一次运行状态
- ``next_run_at``: 下次预计执行时间，worker 据此查询到期 agent
- ``idx_agents_schedule_due``: (schedule_enabled, next_run_at) 覆盖索引

0001 基线已冻结，本迁移用 ALTER 增量补列，不重建基线。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("schedule", sa.String(length=128), nullable=True))
    op.add_column(
        "agents",
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("agents", sa.Column("last_run_at", sa.DateTime(), nullable=True))
    op.add_column(
        "agents", sa.Column("last_run_status", sa.String(length=32), nullable=True)
    )
    op.add_column("agents", sa.Column("last_run_error", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("next_run_at", sa.DateTime(), nullable=True))
    op.create_index(
        "idx_agents_schedule_due", "agents", ["schedule_enabled", "next_run_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_agents_schedule_due", table_name="agents")
    op.drop_column("agents", "next_run_at")
    op.drop_column("agents", "last_run_error")
    op.drop_column("agents", "last_run_status")
    op.drop_column("agents", "last_run_at")
    op.drop_column("agents", "schedule_enabled")
    op.drop_column("agents", "schedule")
