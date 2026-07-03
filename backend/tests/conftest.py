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

import contextlib
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

import app.main as app_main
from app.core.config import settings
from app.core.database import Base, get_session
from app.core.deps import get_current_admin, get_current_user
from app.core.security import hash_password
from app.domains.auth.models import User
from app.main import app

# 强制无 LLM API key，避免 embedder / LLMClient 发起真实网络请求。
settings.openai_api_key = ""
settings.anthropic_api_key = ""
# debug=False 使 Starlette ServerErrorMiddleware 走自定义 Exception handler
# 而非明文 traceback（errors.spec.md§5.4）。建表由 lifespan 无条件执行。
settings.debug = False
app.debug = False


@pytest.fixture(autouse=True)
def _skip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试环境跳过限流（无 Redis 连接）。

    使 ``get_redis`` 立即抛 ``ConnectionRefusedError``，中间件降级放行，
    避免每个请求等待 Redis 连接超时。限流逻辑由 ``test_core_rate_limit.py``
    用 fakeredis 独立测试。
    """
    def _raise() -> None:
        raise ConnectionRefusedError("no redis in test env")

    monkeypatch.setattr("app.core.rate_limit.get_redis", _raise)


def _import_all_orm_models() -> None:
    """触发所有领域 ORM 注册，保证 ``Base.metadata`` 完整。"""
    from app.domains import agents, analytics, auth, evals, knowledge, models, prompts  # noqa: F401


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
            # security.spec.md§3 — 默认以 admin 身份运行既有功能测试，
            # 使各 domain 路由的认证依赖无需逐个传 token；
            # 401/403 边界测试用 ``anon_client`` fixture 关闭本覆盖。
            default_admin = _create_default_admin(c)
            app.dependency_overrides[get_current_user] = lambda: default_admin
            app.dependency_overrides[get_current_admin] = lambda: default_admin
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_current_admin, None)
        # lifespan 关闭时已对 test_engine 执行 dispose，此处兜底再清理一次
        import asyncio

        with contextlib.suppress(Exception):
            asyncio.run(test_engine.dispose())


def _create_default_admin(client: TestClient) -> User:
    """在测试 DB 中创建一个 admin 用户，供 ``client`` fixture 的认证覆盖使用。

    直接经测试会话工厂写入（不走 /auth/register，避免对该端点形成循环依赖）。
    """
    session_factory = app.dependency_overrides[get_session]

    async def _make() -> User:
        user = User(
            email="default-admin@test.local",
            username="default_admin",
            hashed_password=hash_password("DefaultAdmin123!"),
            is_active=True,
            is_admin=True,
        )
        async for session in session_factory():
            session.add(user)
            await session.commit()
            await session.refresh(user)
            break
        return user

    return client.portal.call(_make)


@pytest.fixture
def healthy_deps(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """模拟依赖（DB/Redis）均可达，用于 /health ok 路径测试。

    测试环境无真实 Redis，且 /health 默认会探测依赖；本 fixture 把
    ``check_db`` / ``check_redis`` 桩为 True，使 ok 路径测试无需真实依赖。
    degraded 路径测试自行 monkeypatch 单项为 False。
    """
    from unittest.mock import AsyncMock

    import app.core.health as health_mod

    monkeypatch.setattr(health_mod, "check_db", AsyncMock(return_value=True))
    monkeypatch.setattr(health_mod, "check_redis", AsyncMock(return_value=True))


@pytest.fixture
def anon_client(client: TestClient) -> TestClient:
    """关闭默认认证覆盖，使用真实认证依赖（401/403 边界测试用）。

    依赖 ``client`` 以共享同一测试 DB 与 ``get_session`` 覆盖，仅移除
    ``get_current_user`` / ``get_current_admin`` 覆盖，使请求必须携带有效
    Bearer token 方能通过认证。
    """
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_admin, None)
    return client


@pytest.fixture
def user_client(client: TestClient) -> TestClient:
    """以普通用户（is_admin=False）身份运行，admin 端点应返回 403。

    依赖 ``client`` 共享测试 DB 与 ``get_session`` 覆盖；覆盖
    ``get_current_user`` 为普通用户，并移除 ``get_current_admin`` 覆盖
    使其走真实链路（调用 get_current_user → 校验 is_admin → 403）。
    """
    regular_user = _create_regular_user(client)
    app.dependency_overrides[get_current_user] = lambda: regular_user
    app.dependency_overrides.pop(get_current_admin, None)
    return client


def _create_regular_user(client: TestClient) -> User:
    """在测试 DB 中创建一个普通用户（is_admin=False），供 ``user_client`` 使用。"""
    session_factory = app.dependency_overrides[get_session]

    async def _make() -> User:
        user = User(
            email="regular-user@test.local",
            username="regular_user",
            hashed_password=hash_password("RegularUser123!"),
            is_active=True,
            is_admin=False,
        )
        async for session in session_factory():
            session.add(user)
            await session.commit()
            # expunge 使 user 脱离 session（detach），避免跨 session refresh 冲突；
            # server defaults（created_at/updated_at）已由 INSERT RETURNING 填充。
            session.expunge(user)
            break
        return user

    return client.portal.call(_make)
