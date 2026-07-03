"""ORM vs DB schema 一致性校验（specs/migration.spec.md §8）。

CI 在 ``alembic upgrade head`` 后调用。断言：
- DB 表集合 == ORM ``Base.metadata.tables`` 键集合（无缺失/多余表）。
- 每张表的列名集合一致。

刻意不校验索引 / server_default / 约束名，以避免 autogenerate 反射噪声导致的
误报（基线迁移 0001 由 ``Base.metadata.create_all`` 派生，表/列按构造一致）。
真正的 schema 漂移（缺表/缺列/多列）会被本脚本捕获。

用法：DATABASE_URL=postgresql+asyncpg://... python scripts/check_schema_consistency.py
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
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


def _check(conn) -> None:  # type: ignore[no-untyped-def]
    insp = inspect(conn)
    db_tables = set(insp.get_table_names())
    orm_tables = set(Base.metadata.tables)

    missing = orm_tables - db_tables
    extra = db_tables - orm_tables
    assert not missing, f"ORM 有而 DB 无的表: {sorted(missing)}"
    assert not extra, f"DB 有而 ORM 无的表: {sorted(extra)}"

    for tname in sorted(orm_tables):
        db_cols = {c["name"] for c in insp.get_columns(tname)}
        orm_cols = set(Base.metadata.tables[tname].columns.keys())
        assert db_cols == orm_cols, (
            f"表 {tname} 列不一致 -> ORM 仅缺: {sorted(orm_cols - db_cols)}, "
            f"DB 仅缺: {sorted(db_cols - orm_cols)}"
        )


async def main() -> None:
    eng = create_async_engine(settings.database_url)
    try:
        async with eng.connect() as conn:
            await conn.run_sync(_check)
    finally:
        await eng.dispose()
    print("schema consistency OK: ORM 与 DB 表/列一致")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
