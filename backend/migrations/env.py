"""Alembic 迁移环境 — async (asyncpg) + ORM 单一真源。

对齐 `specs/migration.spec.md` §3/§8：
- target_metadata = `Base.metadata`，所有迁移 autogenerate 均以此为真源。
- 导入全部领域 ORM 模型，确保 metadata 完整。
- sqlalchemy.url 从 `app.core.config.settings.database_url` 注入（生产/CI 由环境变量决定）。

运行方式（在 backend/ 目录下）：
    alembic upgrade head      # 应用到最新
    alembic revision --autogenerate -m "<描述>"   # 生成新迁移
    alembic downgrade -1      # 回滚一步
    alembic check             # ORM vs DB 一致性校验（CI 用）
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.core.database import Base

# 触发全部领域 ORM 注册，保证 Base.metadata 完整（含 auth/users）。
from app.domains import (  # noqa: F401
    agents,
    analytics,
    auth,
    evals,
    knowledge,
    models,
    prompts,
)

config = context.config

# 日志配置（alembic.ini [loggers]）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 注入运行时 DATABASE_URL（覆盖 alembic.ini 中的空值）
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本而不连接 DB。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步连接上配置 context 并执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：用 async engine 建连，run_sync 执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async def _run() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
        await connectable.dispose()

    asyncio.run(_run())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
