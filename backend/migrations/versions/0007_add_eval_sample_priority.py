"""add priority column to eval_samples for C5 stratified sampling

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-04

C5 在线采样改分层 + 优先策略：为 ``eval_samples`` 表新增 ``priority`` 列。

设计要点：
- ``priority`` (INTEGER, default 0) 由 ``execute_agent`` 采样钩子按启发式计算
  （长输入 / self-heal 触发 / 低 eval_score 加分），用于：
  ① 采集时分层采样（priority>0 的请求采样率 boost 倍）；
  ② 评估时优先选取（``list_samples`` / ``run_online_eval`` 按 priority DESC 排序）。
- 新增 ``idx_eval_samples_priority`` 索引支撑优先级排序与过滤。
- 增量 ALTER TABLE，不重建基线，老样本 priority 默认 0（视为普通优先级）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_samples",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "idx_eval_samples_priority", "eval_samples", ["priority"]
    )


def downgrade() -> None:
    op.drop_index(
        "idx_eval_samples_priority", table_name="eval_samples"
    )
    op.drop_column("eval_samples", "priority")
