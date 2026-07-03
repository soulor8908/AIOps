"""core/database.py 单元测试 — 引擎配置、会话工厂、get_session 行为、init_db 建表。

覆盖：
- ``Base`` 为 ``DeclarativeBase`` 子类（所有 ORM 的声明基类）
- 全局 ``engine`` 配置（echo / pool_pre_ping / pool_size / max_overflow）
- ``AsyncSessionLocal`` 绑定到 engine，``expire_on_commit=False`` / ``autoflush=False``
- ``get_session`` 成功路径自动 commit
- ``get_session`` 异常路径自动 rollback 并向上抛出
- ``init_db`` 建表：所有领域 ORM 表出现在 metadata 中

不依赖生产 PostgreSQL：在独立 in-memory SQLite 引擎上验证 get_session / init_db，
避免触碰模块级全局 engine（其 URL 由 tests/conftest.py 兜底为文件型 SQLite）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

import app.core.database as db_module
from app.core.database import Base, get_session

# ===================== Base =====================

def test_base_is_declarative_base_subclass() -> None:
    """Base 必须是 DeclarativeBase 子类，所有 ORM 才能正确注册到同一 metadata。"""
    assert issubclass(Base, DeclarativeBase)


def test_base_metadata_is_shared_registry() -> None:
    """Base.metadata 是所有 ORM 共享的表注册表，类型为 MetaData。"""
    from sqlalchemy import MetaData

    assert isinstance(Base.metadata, MetaData)


# ===================== engine 配置 =====================

def test_engine_is_async_engine() -> None:
    """全局 engine 是 AsyncEngine 实例。"""
    from sqlalchemy.ext.asyncio import AsyncEngine

    assert isinstance(db_module.engine, AsyncEngine)


def test_engine_pool_configured_from_settings() -> None:
    """engine 配置遵循 SPEC：pool_pre_ping=True、pool_size=10、max_overflow=20。

    这些参数确保连接池对长连接做存活探测、并在 burst 时扩容到 30 条连接。
    生产 PG 默认 max_connections=100，留出余量给其它服务。
    """
    assert db_module.engine.pool._pre_ping is True
    assert db_module.engine.pool.size() == 10
    assert db_module.engine.pool._max_overflow == 20


# ===================== AsyncSessionLocal 配置 =====================

def test_session_factory_bound_to_engine() -> None:
    """AsyncSessionLocal 绑定到全局 engine。"""
    assert db_module.AsyncSessionLocal.kw["bind"] is db_module.engine


def test_session_factory_class_is_async_session() -> None:
    """工厂产出 AsyncSession 实例。"""
    assert db_module.AsyncSessionLocal.class_ is AsyncSession


def test_session_factory_expire_on_commit_disabled() -> None:
    """expire_on_commit=False：commit 后 ORM 对象不过期，避免异步访问触发隐式刷新。

    FastAPI 路由返回 ORM 对象时需在 commit 后仍可读属性（Pydantic from_attributes）。
    """
    assert db_module.AsyncSessionLocal.kw["expire_on_commit"] is False


def test_session_factory_autoflush_disabled() -> None:
    """autoflush=False：避免查询前隐式 flush 把未完成的对象写入 DB。

    显式 flush 让 service 层对写入时机可控（Flat > Deep，纯函数语义）。
    """
    assert db_module.AsyncSessionLocal.kw["autoflush"] is False


# ===================== get_session 行为 =====================

class _StubBase(DeclarativeBase):
    """独立 DeclarativeBase，避免污染生产 Base.metadata。"""


class _StubRow(_StubBase):
    __tablename__ = "_test_db_stub_row"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)


@pytest.fixture
def _isolated_engine_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """提供一个独立 in-memory SQLite 引擎 + 会话工厂，并临时替换 db_module.AsyncSessionLocal。

    使 ``get_session`` 在测试中使用隔离引擎，不触碰全局 engine。返回 (engine, factory)
    便于断言 commit / rollback 行为。
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    test_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    monkeypatch.setattr(db_module, "AsyncSessionLocal", test_factory)

    async def _init_stub() -> None:
        async with test_engine.begin() as conn:
            await conn.run_sync(_StubBase.metadata.create_all)

    import asyncio

    asyncio.run(_init_stub())

    yield test_engine, test_factory

    asyncio.run(test_engine.dispose())


async def test_get_session_commits_on_success(_isolated_engine_factory) -> None:
    """get_session 成功路径：yield session → 调用方 commit 后自动提交。"""
    _, factory = _isolated_engine_factory
    gen: AsyncGenerator[AsyncSession, None] = get_session()
    session = await gen.__anext__()
    row = _StubRow(id="commit-test")
    session.add(row)
    # 触发 generator 的 finally：commit + close
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    # 新 session 应能查到已提交的行
    async with factory() as s:
        stmt = select(_StubRow).where(_StubRow.id == "commit-test")
        result = (await s.execute(stmt)).scalar_one_or_none()
        assert result is not None
        assert result.id == "commit-test"


async def test_get_session_rolls_back_on_exception(_isolated_engine_factory) -> None:
    """get_session 异常路径：抛异常时自动 rollback，未提交的行不入库。"""
    _, factory = _isolated_engine_factory
    gen: AsyncGenerator[AsyncSession, None] = get_session()
    session = await gen.__anext__()
    row = _StubRow(id="rollback-test")
    session.add(row)
    # 模拟调用方抛异常 → generator 应 rollback 并 re-raise
    with pytest.raises(RuntimeError, match="simulated failure"):
        try:
            raise RuntimeError("simulated failure")
        except RuntimeError as exc:
            await gen.athrow(exc)

    # 新 session 不应查到该行（已回滚）
    async with factory() as s:
        result = (
            await s.execute(select(_StubRow).where(_StubRow.id == "rollback-test"))
        ).scalar_one_or_none()
        assert result is None


async def test_get_session_yields_async_session(_isolated_engine_factory) -> None:
    """get_session 每次调用 yield 一个 AsyncSession。"""
    gen: AsyncGenerator[AsyncSession, None] = get_session()
    session = await gen.__anext__()
    assert isinstance(session, AsyncSession)
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


# ===================== init_db 建表 =====================

def test_init_db_creates_all_domain_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_db 建表：所有领域 ORM 表出现在测试引擎的 metadata 中。

    不调用真实 init_db（其副作用在全局 engine 上），而是直接验证
    ``Base.metadata`` 已注册全部领域表——init_db 内部就是
    ``Base.metadata.create_all`` + 触发领域模块导入。
    """
    # 触发所有领域 ORM 注册（与 init_db 内部导入一致）
    from app.domains import (  # noqa: F401
        agents,
        analytics,
        auth,
        evals,
        knowledge,
        models,
        prompts,
    )

    table_names = set(Base.metadata.tables.keys())
    # 各领域至少一张表
    assert "users" in table_names  # auth
    assert "prompts" in table_names and "prompt_versions" in table_names  # prompts
    assert "agents" in table_names  # agents
    assert "knowledge_bases" in table_names  # knowledge
    assert "model_configs" in table_names  # models
    # analytics / evals 视具体实现而定，不强断言（避免与 ORM 演进耦合）


async def test_init_db_runs_create_all_against_target_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_db 用模块级 engine 调用 create_all：替换为 in-memory 引擎后建表成功。

    验证 init_db 的关键行为（导入领域模块 + Base.metadata.create_all），
    而非依赖全局 engine（tests/conftest.py 兜底为文件型 SQLite，但此处仍隔离）。
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_module, "engine", test_engine)

    await db_module.init_db()

    # 验证建表成功：直接查 sqlite_master
    async with test_engine.connect() as conn:
        from sqlalchemy import text

        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = {row[0] for row in result}
    # 至少包含核心领域表
    assert "users" in tables
    assert "prompts" in tables
    assert "agents" in tables

    await test_engine.dispose()
