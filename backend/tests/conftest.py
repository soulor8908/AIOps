"""L2 API 契约测试共享 fixture。

为 ``TestClient(app)`` 集成测试提供 SQLite in-memory 后端：

- 每个测试函数获得一个全新的 SQLite in-memory 引擎（``StaticPool`` 单连接），
  保证跨请求可见性且测试间完全隔离。
- 重定向 ``app.main`` 的 lifespan：用测试引擎建表、用测试引擎 dispose，
  避免命中生产 PostgreSQL（pgvector 扩展不可用）。
- 覆盖 ``get_session`` 依赖，使所有请求走测试会话。
- 强制清空 LLM API key，杜绝真实网络调用。

环境兼容说明
------------
根目录 ``conftest.py`` 会 ``setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")``，
而 ``app.core.database`` 的模块级 ``create_async_engine`` 传入了 ``pool_size`` /
``max_overflow``——这两个参数与 SQLite ``:memory:`` 自动选择的 ``StaticPool``
不兼容，会导致 ``app.core.database`` 导入即抛 ``TypeError``。

因此本文件在导入 ``app`` 之前，把 ``DATABASE_URL`` 显式覆盖为 **文件型 SQLite**
URL（文件型走 ``QueuePool``，接受 ``pool_size``），使全局引擎能成功创建。
该全局引擎在测试中从不连接（请求走测试会话、lifespan 建表走测试引擎），
仅为满足导入期约束。实际测试数据落在测试用 ``:memory:`` 引擎上。

根目录 ``conftest.py`` 已在 SQLite 方言上把 JSONB / VECTOR 渲染为 JSON、
注册 ``date_trunc`` UDF，本文件不重复其逻辑。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

# ---------- 必须在导入 app 之前覆盖 DATABASE_URL ----------
# 文件型 SQLite 使 app.core.database 的 pool_size/max_overflow 合法；
# 全局引擎从不连接，仅用于通过模块导入。
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/aiops_global_engine.db"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.database import Base, get_session
from app.main import app
import app.main as app_main

# 强制无 LLM API key，避免 embedder / LLMClient 发起真实网络请求。
settings.openai_api_key = ""
settings.anthropic_api_key = ""
# 确保 lifespan 中 ``if settings.debug: await init_db()`` 执行建表。
settings.debug = True


def _import_all_orm_models() -> None:
    """触发所有领域 ORM 注册，保证 ``Base.metadata`` 完整。"""
    from app.domains import agents, analytics, evals, knowledge, models, prompts  # noqa: F401


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[TestClient, None]:
    """每个测试一个独立的 SQLite in-memory TestClient。"""
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    test_session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async def _test_init_db() -> None:
        """lifespan 启动时在测试引擎上建表（与请求同事件循环）。"""
        _import_all_orm_models()
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # 重定向 lifespan 使用的模块级名称到测试引擎
    monkeypatch.setattr(app_main, "init_db", _test_init_db)
    monkeypatch.setattr(app_main, "engine", test_engine)
    # 所有请求走测试会话
    app.dependency_overrides[get_session] = _override_get_session

    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        # lifespan 关闭时已对 test_engine 执行 dispose，此处兜底再清理一次
        import asyncio

        try:
            asyncio.run(test_engine.dispose())
        except Exception:
            pass
