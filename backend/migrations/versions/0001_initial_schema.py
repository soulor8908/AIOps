"""initial schema baseline (ORM-driven)

Revision ID: 0001
Revises:
Create Date: 2026-07-03

Alpha 基线策略（specs/migration.spec.md §5）:
- 本迁移用 ``Base.metadata.create_all`` / ``drop_all``，使 ORM 成为字面真源——
  无转写漂移，CI 一致性校验（ORM vs DB）按构造即通过。
- 在 v0.1.0 冻结前，schema 变更应**重建本基线**（替换 0001）而非叠加 ALTER
  迁移，以规避 ``create_all`` 使用运行时 ORM 的演进隐患。
- v0.1.0 冻结后，改用显式 ``op.create_table`` / ``op.alter_column`` 迁移，
  0001 保持冻结作为历史快照。

Revision ID: 0001
Revises:
Create Date: 2026-07-03
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.database import Base

# 触发全部领域 ORM 注册，确保 Base.metadata 完整。
from app.domains import (  # noqa: F401
    agents,
    analytics,
    auth,
    evals,
    knowledge,
    models,
    prompts,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ORM 单一真源：从 Base.metadata 创建全部表/约束/索引（含 HNSW 向量索引）。
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
