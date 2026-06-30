"""FastAPI 依赖注入 — 认证与授权。

- ``oauth2_scheme``：Bearer token 提取器（auto_error=False，缺失时由
  ``get_current_user`` 统一抛 ``AuthenticationError``，避免 FastAPI 默认格式）
- ``get_current_user``：解析 token → 查 users 表 → 返回 User
- ``get_current_admin``：在 ``get_current_user`` 基础上校验 ``is_admin``
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.jwt import verify_token

if TYPE_CHECKING:
    from app.domains.auth.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """解析 Bearer token → 查 users 表 → 返回 User。延迟导入避免循环。"""
    from app.domains.auth.models import User

    if not token:
        raise AuthenticationError("未提供认证凭据")
    user_id = verify_token(token)
    try:
        uid = UUID(user_id)
    except ValueError as exc:
        raise AuthenticationError("token subject 非合法 UUID") from exc
    user = (
        await session.execute(select(User).where(User.id == uid))
    ).scalar_one_or_none()
    if user is None:
        raise AuthenticationError("用户不存在")
    if not user.is_active:
        raise AuthenticationError("用户已停用")
    return user


async def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    """在 ``get_current_user`` 基础上校验 ``is_admin``。"""
    if not user.is_admin:
        raise AuthorizationError("需要管理员权限")
    return user
