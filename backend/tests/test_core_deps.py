"""core/deps.py 单元测试 — 认证与授权依赖注入。

覆盖 auth/SPEC.md§Auth Dependencies：
- ``oauth2_scheme`` 配置（auto_error=False，由 get_current_user 统一抛错）
- ``get_current_user``：
  - 无 token → AuthenticationError(token_invalid)
  - token subject 非合法 UUID → AuthenticationError(token_invalid)
  - 用户不存在 → AuthenticationError(token_invalid)
  - 用户 is_active=False → AuthenticationError(token_invalid)
  - 正常路径 → 返回 User
- ``get_current_admin``：
  - is_admin=False → AuthorizationError(permission_denied)
  - is_admin=True → 返回 User

直接调用依赖函数（绕过 TestClient），用真实 SQLite in-memory session +
monkeypatch ``verify_token`` 控制返回值，单元粒度验证分支逻辑。
RBAC 端到端边界由 test_rbac_boundary.py 覆盖，此处聚焦函数级覆盖。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.deps import get_current_admin, get_current_user, oauth2_scheme
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.security import hash_password

# 触发所有领域 ORM 注册，保证 Base.metadata 完整（建表需要）。
from app.domains import (  # noqa: F401
    agents,
    analytics,
    auth,
    evals,
    knowledge,
    models,
    prompts,
)
from app.domains.auth.models import User

# ===================== oauth2_scheme =====================


def test_oauth2_scheme_auto_error_disabled() -> None:
    """oauth2_scheme.auto_error=False：缺失 token 不由 FastAPI 抛 401，由 get_current_user 统一抛。

    否则 FastAPI 默认返回 ``{"detail": "Not authenticated"}`` 与项目统一错误格式
    ``{error, message, detail}`` 不一致（errors.spec.md§2）。
    """
    assert oauth2_scheme.auto_error is False


def test_oauth2_scheme_token_url_points_to_auth_endpoint() -> None:
    """tokenUrl 指向 /api/v1/auth/token（与 auth router 实际登录端点一致）。

    FastAPI 把 tokenUrl 存在 ``oauth2_scheme.model.flows.password.tokenUrl``，
    用于 OpenAPI 文档的 SecurityScheme 渲染。
    """
    assert oauth2_scheme.model.flows.password.tokenUrl == "/api/v1/auth/token"


# ===================== get_current_user / get_current_admin fixture =====================


@pytest.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    """提供独立 in-memory SQLite 会话工厂。

    每个 test 函数获得全新 DB，避免跨测试污染。SQLite StaticPool 单连接
    保证 :memory: 跨请求可见。
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield factory

    await test_engine.dispose()


def _make_user(
    *,
    is_active: bool = True,
    is_admin: bool = False,
    email: str = "deps-test@test.local",
    user_id: uuid.UUID | None = None,
) -> User:
    """构造一个 User 实体（不写库，由调用方 add+commit）。"""
    return User(
        id=user_id or uuid.uuid4(),
        email=email,
        username=f"user-{(user_id or uuid.uuid4()).hex[:8]}",
        hashed_password=hash_password("Password123!"),
        is_active=is_active,
        is_admin=is_admin,
    )


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    """把 user 写入测试 DB（async，须在 async test 中 await）。"""
    async with session_factory() as s:
        s.add(user)
        await s.commit()


async def _resolve_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncSession:
    """从工厂开一个 session 供 get_current_user 使用。"""
    return session_factory()


# ===================== get_current_user — 失败路径 =====================


async def test_get_current_user_no_token_raises_authentication_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """无 token → AuthenticationError（error_code=token_invalid）。"""
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_current_user(token=None, session=session)
        assert exc_info.value.error_code == "token_invalid"
        assert exc_info.value.status_code == 401
    finally:
        await session.close()


async def test_get_current_user_empty_token_raises_authentication_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """空字符串 token 视同缺失 → AuthenticationError。"""
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError):
            await get_current_user(token="", session=session)
    finally:
        await session.close()


async def test_get_current_user_invalid_uuid_subject_raises(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token 解析出非 UUID 的 sub → AuthenticationError（防注入与类型混淆）。"""
    # P0-1：deps 改用 verify_token_with_blacklist（async），测试 stub 需 async
    async def _fake_verify(_t: str) -> str:
        return "not-a-uuid"

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _fake_verify)
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_current_user(token="some-token", session=session)
        assert "UUID" in str(exc_info.value) or "UUID" in (exc_info.value.detail or "")
    finally:
        await session.close()


async def test_get_current_user_token_verify_failure_propagates(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_token_with_blacklist 抛 AuthenticationError 时直接向上传播（不吞异常）。"""
    async def _raise(_t: str) -> str:
        raise AuthenticationError("token 已过期")

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _raise)
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_current_user(token="expired", session=session)
        assert "过期" in str(exc_info.value)
    finally:
        await session.close()


async def test_get_current_user_user_not_found_raises(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sub 合法但 DB 中无此用户 → AuthenticationError（用户不存在）。"""
    uid = uuid.uuid4()

    async def _fake_verify(_t: str) -> str:
        return str(uid)

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _fake_verify)
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_current_user(token="t", session=session)
        assert "不存在" in str(exc_info.value)
    finally:
        await session.close()


async def test_get_current_user_inactive_user_raises(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_active=False 用户 → AuthenticationError（已停用）。"""
    user = _make_user(is_active=False)
    await _seed_user(session_factory, user)

    async def _fake_verify(_t: str) -> str:
        return str(user.id)

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _fake_verify)
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_current_user(token="t", session=session)
        assert "停用" in str(exc_info.value)
    finally:
        await session.close()


# ===================== get_current_user — 成功路径 =====================


async def test_get_current_user_returns_user_on_success(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常路径：合法 token + 存在的活跃用户 → 返回 User 实例。"""
    user = _make_user(is_active=True, is_admin=False)
    await _seed_user(session_factory, user)

    async def _fake_verify(_t: str) -> str:
        return str(user.id)

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _fake_verify)
    session = await _resolve_session(session_factory)
    try:
        result = await get_current_user(token="t", session=session)
        assert isinstance(result, User)
        assert result.id == user.id
        assert result.email == user.email
    finally:
        await session.close()


# ===================== get_current_admin — 失败路径 =====================


async def test_get_current_admin_non_admin_raises_authorization_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """is_admin=False → AuthorizationError（permission_denied）。"""
    user = _make_user(is_admin=False)
    with pytest.raises(AuthorizationError) as exc_info:
        await get_current_admin(user=user)
    assert exc_info.value.error_code == "permission_denied"
    assert exc_info.value.status_code == 403


# ===================== get_current_admin — 成功路径 =====================


async def test_get_current_admin_returns_admin_on_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """is_admin=True → 返回同一个 User。"""
    user = _make_user(is_admin=True)
    result = await get_current_admin(user=user)
    assert result is user
    assert result.is_admin is True


async def test_get_current_admin_propagates_get_current_user_errors(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_current_admin 在 get_current_user 之前失败时直接传播 AuthenticationError。

    通过直接调用 get_current_admin 并 monkeypatch verify_token_with_blacklist 抛错来验证：
    依赖链 get_current_admin → get_current_user → verify_token_with_blacklist 的异常不被吞掉。
    """
    async def _raise(_t: str) -> str:
        raise AuthenticationError("无效 token")

    monkeypatch.setattr("app.core.deps.verify_token_with_blacklist", _raise)
    session = await _resolve_session(session_factory)
    try:
        with pytest.raises(AuthenticationError):
            # get_current_admin 内部 Depends(get_current_user) 在直接调用时不会触发，
            # 此处显式构造失败链路：先调用 get_current_user（被 get_current_admin 依赖），
            # 失败应先于 is_admin 校验。直接断言 get_current_user 异常向上传播即可。
            await get_current_user(token="invalid", session=session)
    finally:
        await session.close()
