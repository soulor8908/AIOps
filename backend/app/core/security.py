"""安全模块 — JWT + OAuth2 + 密码哈希 + RBAC 依赖（极简实现）。

覆盖 `auth/SPEC.md`§Auth Dependencies 与 `specs/security.spec.md`§2/§3：
- access/refresh token 双 token（JWT claim ``type`` 区分）
- ``verify_token`` 单独捕获 ``ExpiredSignatureError`` 抛 ``TokenExpiredError``
- ``get_current_user`` / ``get_current_admin`` FastAPI 依赖注入
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.exceptions import AuthenticationError, AuthorizationError, TokenExpiredError

if TYPE_CHECKING:
    from app.domains.auth.models import User

ALGORITHM: Literal["HS256"] = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# auto_error=False 让缺失 token 由 get_current_user 统一抛 AuthenticationError，
# 而非 FastAPI 默认 401 + {detail: "Not authenticated"}（违反 errors.spec.md§2）。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def hash_password(plain: str) -> str:
    """bcrypt 哈希明文密码。"""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希。"""
    return pwd_context.verify(plain, hashed)


def _encode(payload: dict[str, object]) -> str:
    return jwt.encode(payload, settings.effective_secret_key, algorithm=ALGORITHM)


def create_access_token(subject: str, expires_seconds: int | None = None) -> str:
    """签发 access token。subject 通常是 user_id 字符串。"""
    expire = datetime.now(UTC) + timedelta(
        seconds=expires_seconds or settings.access_token_expire_seconds
    )
    payload: dict[str, object] = {
        "sub": subject,
        "exp": expire,
        "type": TOKEN_TYPE_ACCESS,
    }
    return _encode(payload)


def create_refresh_token(subject: str, expires_seconds: int | None = None) -> str:
    """签发 refresh token（默认 7d，可配置 `REFRESH_TOKEN_EXPIRE_DAYS`）。"""
    expire = datetime.now(UTC) + timedelta(
        seconds=expires_seconds or settings.refresh_token_expire_seconds
    )
    payload: dict[str, object] = {
        "sub": subject,
        "exp": expire,
        "type": TOKEN_TYPE_REFRESH,
    }
    return _encode(payload)


def decode_token(token: str) -> dict[str, object]:
    """校验并解析 JWT，返回完整 payload。

    - 过期 → ``TokenExpiredError``（``error_code="token_expired"``）
    - 其它无效 → ``AuthenticationError``（``error_code="token_invalid"``）
    """
    try:
        return jwt.decode(token, settings.effective_secret_key, algorithms=[ALGORITHM])
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("认证凭据已过期") from exc
    except JWTError as exc:
        raise AuthenticationError(f"认证凭据无效: {exc}") from exc


def verify_token(token: str) -> str:
    """校验 access token 并返回 subject（user_id）。

    保留作为 ``auth/SPEC.md`` 文档化的便捷入口；新代码优先用 ``decode_token``
    以获取 ``type`` 等字段。拒绝 refresh token 类型。
    """
    payload = decode_token(token)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AuthenticationError("token 缺少 sub")
    token_type = payload.get("type")
    if token_type == TOKEN_TYPE_REFRESH:
        # access 端点不应接受 refresh token
        raise AuthenticationError("token 类型错误")
    return sub


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI 依赖：解析 Bearer token → 查 users 表 → 返回 User。

    延迟导入 User 避免与 ``app.domains.auth`` 形成顶层循环。
    """
    from app.domains.auth.models import User

    if not token:
        raise AuthenticationError("未提供认证凭据")
    user_id = verify_token(token)
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise AuthenticationError("token subject 非合法 UUID") from exc
    stmt = select(User).where(User.id == uid)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise AuthenticationError("用户不存在")
    if not user.is_active:
        raise AuthenticationError("用户已停用")
    return user


async def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI 依赖：在 ``get_current_user`` 基础上校验 ``is_admin``。"""
    if not user.is_admin:
        raise AuthorizationError("需要管理员权限")
    return user


__all__ = [
    "ALGORITHM",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_admin",
    "get_current_user",
    "hash_password",
    "oauth2_scheme",
    "verify_password",
    "verify_token",
]
