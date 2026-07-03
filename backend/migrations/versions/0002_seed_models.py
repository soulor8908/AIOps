"""seed default model_configs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-03

数据迁移：写入默认模型配置（原 init.sql 种子）。幂等（ON CONFLICT DO NOTHING）。
对应 specs/migration.spec.md §4（种子数据走 Alembic 数据迁移）。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 默认模型配置：alias / provider / model_name / max_tokens / priority
_SEEDS = [
    ("default", "openai", "gpt-4o-mini", 4096, 100),
    ("gpt-4o", "openai", "gpt-4o", 4096, 90),
    ("claude-3.5", "anthropic", "claude-3-5-sonnet-20241022", 4096, 80),
]
_SEED_ALIASES = tuple(alias for alias, *_ in _SEEDS)


def upgrade() -> None:
    values_sql = ", ".join(
        f"('{alias}','{provider}','{model_name}',{max_tokens},{priority})"
        for alias, provider, model_name, max_tokens, priority in _SEEDS
    )
    op.execute(
        f"INSERT INTO model_configs (alias, provider, model_name, max_tokens, priority) "
        f"VALUES {values_sql} ON CONFLICT (alias) DO NOTHING"
    )


def downgrade() -> None:
    aliases = ", ".join(f"'{a}'" for a in _SEED_ALIASES)
    op.execute(f"DELETE FROM model_configs WHERE alias IN ({aliases})")
