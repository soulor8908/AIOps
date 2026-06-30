"""Auth service — 注册 / 登录 / 刷新的业务逻辑。

遵循 ``auth/SPEC.md``：
- 注册：email 归一化小写、bcrypt 哈希、唯一冲突 → ConflictError
- 登录：按 email 查用户、verify_password、签发 access+refresh token
- 刷新：校验 refresh token type、签发新 token 对（refresh 轮换）
"""

from __future__ import annotations

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
from app.domains.auth.models import Token, User, UserCreate, UserOut


async def register_user(session: AsyncSession, data: UserCreate) -> User:
    """注册新用户。email 归一化为小写，密码 bcrypt 哈希后入库。

    - email / username 唯一冲突 → ``ConflictError`` (409)
    """
    user = User(
        email=data.email.lower(),
        username=data.username,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
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


async def authenticate_user(
    session: AsyncSession, email: str, password: str
) -> User:
    """校验 email + password，返回 User。

    - 用户不存在 → ``AuthenticationError``
    - 密码不匹配 → ``AuthenticationError``
    - 用户停用 → ``AuthenticationError``
    """
    stmt = select(User).where(User.email == email.lower())
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
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
    """
    payload = decode_token(refresh_token_str)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AuthenticationError("token 缺少 sub")
    token_type = payload.get("type")
    if token_type != TOKEN_TYPE_REFRESH:
        raise AuthenticationError("token 类型错误：需要 refresh token")
    return Token(
        access_token=create_access_token(sub),
        refresh_token=create_refresh_token(sub),  # 轮换
        expires_in=settings.access_token_expire_seconds,
    )


def to_user_out(user: User) -> UserOut:
    """User ORM → UserOut。"""
    return UserOut.from_orm_user(user)
