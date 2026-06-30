"""Auth router — /api/v1/auth 端点。

| POST /auth/register | 注册 |
| POST /auth/token    | 登录（OAuth2PasswordRequestForm） |
| GET  /auth/me       | 当前用户（需 Bearer） |
| POST /auth/refresh  | 刷新 token |
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.domains.auth.models import (
    RefreshRequest,
    Token,
    UserCreate,
    UserOut,
)
from app.domains.auth.service import (
    authenticate_user,
    issue_token_pair,
    refresh_access_token,
    register_user,
    to_user_out,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    data: UserCreate, session: AsyncSession = Depends(get_session)
) -> UserOut:
    """用户注册。"""
    user = await register_user(session, data)
    return to_user_out(user)


@router.post("/token", response_model=Token)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> Token:
    """登录获取 token（OAuth2PasswordRequestForm: username=email, password）。"""
    user = await authenticate_user(session, form.username, form.password)
    return issue_token_pair(user)


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)) -> UserOut:
    """获取当前登录用户信息。"""
    return to_user_out(user)


@router.post("/refresh", response_model=Token)
async def refresh(data: RefreshRequest) -> Token:
    """使用 refresh token 换取新的 access token（并轮换 refresh token）。"""
    return await refresh_access_token(data.refresh_token)
