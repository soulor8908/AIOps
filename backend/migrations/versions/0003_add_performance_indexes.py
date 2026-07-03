"""add performance indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04

P3 性能优化：为高频聚合查询字段补索引（对应 ORM __table_args__ 变更）。
- ``conversations.model_alias``：dashboard ``_active_models`` GROUP BY 聚合，原 Seq Scan。
- ``prompt_versions (prompt_id, version_num)``：diff / rollback / create_version 高频查询。

0001 基线用 ``create_all`` 派生，新部署已含这些索引；本迁移为已部署环境补建。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_conversations_model_alias", "conversations", ["model_alias"]
    )
    op.create_index(
        "idx_prompt_versions_pid_vnum",
        "prompt_versions",
        ["prompt_id", "version_num"],
    )


def downgrade() -> None:
    op.drop_index("idx_prompt_versions_pid_vnum", table_name="prompt_versions")
    op.drop_index("idx_conversations_model_alias", table_name="conversations")
