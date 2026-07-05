"""Auth service — 注册 / 登录 / 刷新的业务逻辑。

遵循 ``auth/SPEC.md``：
- 注册：email 归一化小写、bcrypt 哈希、唯一冲突 → ConflictError
- 登录：按 email 查用户、verify_password、签发 access+refresh token
- 刷新：校验 refresh token type、签发新 token 对（refresh 轮换）
"""

from __future__ import annotations

import asyncio
import functools

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.jwt import (
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.security import hash_password, verify_password
from app.core.token_blacklist import revoke_token
from app.domains.auth.models import Token, User, UserCreate, UserOut


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """生成固定假密码的 bcrypt 哈希（首次调用惰性计算，之后缓存）。

    用于用户不存在时仍跑一次 bcrypt verify，使响应时延与密码错误场景一致，
    抵抗用户枚举时序攻击。惰性初始化避免导入期 ~200ms 哈希开销阻塞启动。
    """
    return hash_password("dummy-timing-constant")


async def register_user(session: AsyncSession, data: UserCreate) -> User:
    """注册新用户。email 归一化为小写，密码 bcrypt 哈希后入库。

    - email / username 唯一冲突 → ``ConflictError`` (409)

    bcrypt 哈希是 CPU 密集的同步操作（~100-300ms），用 ``asyncio.to_thread``
    卸载到线程池，避免阻塞事件循环导致并发请求被串行化。
    """
    hashed = await asyncio.to_thread(hash_password, data.password)
    user = User(
        email=data.email.lower(),
        username=data.username,
        full_name=data.full_name,
        hashed_password=hashed,
        is_active=True,
        is_admin=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # 判断是 email 还是 username 冲突（原信息在 exc.orig）
        raise ConflictError(
            "email 或 username 已存在",
            detail={"email": data.email, "username": data.username},
        ) from exc
    return user


# 固定假哈希，用于用户不存在时仍跑一次 bcrypt verify 以统一时延，
# 抵抗用户枚举时序攻击。模块级缓存避免每次重新哈希（bcrypt ~100-300ms）。
# 该哈希由 hash_password("dummy-timing-constant") 派生，永不会与真实密码匹配。
_DUMMY_HASH = "$2b$12$CwTycUXWue0Thq9StjUM0uJ8eVjP3wW6Q3vOKvBb6Q3oXvQ9oOaWy"


async def authenticate_user(
    session: AsyncSession, email: str, password: str
) -> User:
    """校验 email + password，返回 User。

    - 用户不存在 → ``AuthenticationError``
    - 密码不匹配 → ``AuthenticationError``
    - 用户停用 → ``AuthenticationError``

    时序攻击防御：用户不存在时仍跑一次 bcrypt verify（对固定假哈希），
    使响应时延与密码错误场景一致，避免攻击者通过时延差异枚举有效邮箱。
    bcrypt 校验是 CPU 密集的同步操作，用 ``asyncio.to_thread`` 卸载。
    """
    stmt = select(User).where(User.email == email.lower())
    user = (await session.execute(stmt)).scalar_one_or_none()
    # 无论 user 是否存在都跑一次 verify，统一时延抵抗用户枚举
    hashed = user.hashed_password if user is not None else _dummy_hash()
    password_ok = await asyncio.to_thread(verify_password, password, hashed)
    if user is None or not password_ok:
        raise AuthenticationError("邮箱或密码错误")
    if not user.is_active:
        raise AuthenticationError("用户已停用")
    return user


def issue_token_pair(user: User) -> Token:
    """为用户签发 access + refresh token 对。"""
    sub = str(user.id)
    return Token(
        access_token=create_access_token(sub),
        refresh_token=create_refresh_token(sub),
        expires_in=settings.access_token_expire_seconds,
    )


async def refresh_access_token(refresh_token_str: str) -> Token:
    """用 refresh token 换取新的 token 对（refresh 轮换）。

    - token 无效/过期 → ``TokenExpiredError`` / ``AuthenticationError``
    - type != refresh → ``AuthenticationError``

    P0-5：轮换时把旧 refresh token 的 jti 加入黑名单（一次性使用），防止
    重放攻击——攻击者拿到旧 refresh token 后无法重复换新 token。
    """
    payload = decode_token(refresh_token_str)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AuthenticationError("token 缺少 sub")
    token_type = payload.get("type")
    if token_type != TOKEN_TYPE_REFRESH:
        raise AuthenticationError("token 类型错误：需要 refresh token")
    # P0-5：旧 refresh token 吊销（一次性使用）。
    jti = payload.get("jti")
    exp = payload.get("exp")
    if isinstance(jti, str):
        await revoke_token(jti, int(exp) if isinstance(exp, (int, float)) else None)
    return Token(
        access_token=create_access_token(sub),
        refresh_token=create_refresh_token(sub),  # 轮换
        expires_in=settings.access_token_expire_seconds,
    )


async def logout(access_token_str: str) -> None:
    """P0-1：登出——把 access token 的 jti 加入黑名单。

    登出后该 access token 在剩余有效期内不可用（Redis 不可用时降级：token
    仍可用直到自然过期，与 blacklist 降级策略一致）。
    """
    payload = decode_token(access_token_str)
    jti = payload.get("jti")
    exp = payload.get("exp")
    if isinstance(jti, str):
        await revoke_token(jti, int(exp) if isinstance(exp, (int, float)) else None)


def to_user_out(user: User) -> UserOut:
    """User ORM → UserOut。"""
    return UserOut.from_orm_user(user)
