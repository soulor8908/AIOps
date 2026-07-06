"""SQLAlchemy 2.0 async 数据库基础设施。

提供：
- Base 声明基类（所有 ORM 继承）
- async engine + AsyncSession 工厂
- init_db 建表函数（开发期）
- get_session FastAPI 依赖
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


# P0-22：PG statement_timeout（30s）防慢查询占满连接池。pgvector 检索
# 未命中索引 / N+1 查询 / LIKE 全表扫描会被 PG 主动 kill，连接归还池。
# ``server_settings`` 是 asyncpg 专属参数，SQLite/aiosqlite 后端不支持，
# 需按 dialect 条件注入（测试用 SQLite，生产用 PostgreSQL）。
_db_connect_args: dict[str, Any] = {}
if settings.database_url.startswith("postgresql"):
    _db_connect_args = {
        "server_settings": {"statement_timeout": str(settings.db_statement_timeout_ms)}
    }

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # P0-11：长连接主动回收（30min），避开 AWS RDS Proxy / 阿里云 SLB 等
    # 中间件 60-300s idle 超时窗口，避免 pre_ping 才发现死连接导致首请求延迟尖峰。
    pool_recycle=settings.db_pool_recycle_seconds,
    # P0-11：连接池耗尽时快速失败（10s），优于默认 30s 雪崩——高并发下池耗尽
    # 时 30s 等待会拖垮整个 worker。
    pool_timeout=settings.db_pool_timeout_seconds,
    connect_args=_db_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：注入 AsyncSession。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """开发期建表（生产用 alembic）。需先导入所有 ORM 模块。"""
    # 触发 ORM 注册（避免循环导入，仅副作用）。含 auth/users，否则 users 表不会被建。
    from app.domains import (  # noqa: F401
        agents,
        analytics,
        auth,
        evals,
        knowledge,
        models,
        prompts,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
