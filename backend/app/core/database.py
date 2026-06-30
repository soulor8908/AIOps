"""SQLAlchemy 2.0 async 数据库基础设施。

提供：
- Base 声明基类（所有 ORM 继承）
- async engine + AsyncSession 工厂
- init_db 建表函数（开发期）
- get_session FastAPI 依赖
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
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
    # 触发 ORM 注册（避免循环导入，仅副作用）
    from app.domains import (  # noqa: F401
        agents,
        analytics,
        evals,
        knowledge,
        models,
        prompts,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
