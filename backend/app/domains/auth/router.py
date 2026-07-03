"""Auth router — /api/v1/auth 端点。

| POST /auth/register | 注册 |
| POST /auth/token    | 登录（OAuth2PasswordRequestForm） |
| GET  /auth/me       | 当前用户（需 Bearer） |
| POST /auth/refresh  | 刷新 token |
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.core.exceptions import AppError
from app.domains.auth.models import (
    RefreshRequest,
    Token,
    User,
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

logger = logging.getLogger("app.audit.auth")

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
    try:
        user = await register_user(session, data)
    except AppError:
        # 审计日志（security.spec.md§可审计性）：注册失败也记录，便于排查异常注册行为。
        # 不记录密码等敏感字段；email/username 作为业务标识需要保留。
        logger.warning(
            "register_failed",
            extra={"event": "register", "outcome": "failure", "email": data.email},
        )
        raise
    logger.info(
        "register_success",
        extra={
            "event": "register",
            "outcome": "success",
            "user_id": str(user.id),
            "email": user.email,
        },
    )
    return to_user_out(user)


@router.post("/token", response_model=Token)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> Token:
    """登录获取 token（OAuth2PasswordRequestForm: username=email, password）。"""
    try:
        user = await authenticate_user(session, form.username, form.password)
    except AppError as exc:
        # 审计日志：登录失败可能表示凭据错误或账户停用，也可能是攻击者撞库。
        # form.username 即用户输入的 email（OAuth2PasswordRequestForm 约定）。
        logger.warning(
            "login_failed",
            extra={
                "event": "login",
                "outcome": "failure",
                "email": form.username,
                "reason": exc.error_code,
            },
        )
        raise
    logger.info(
        "login_success",
        extra={
            "event": "login",
            "outcome": "success",
            "user_id": str(user.id),
            "email": user.email,
        },
    )
    return issue_token_pair(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    """获取当前登录用户信息。"""
    return to_user_out(user)


@router.post("/refresh", response_model=Token)
async def refresh(data: RefreshRequest) -> Token:
    """使用 refresh token 换取新的 access token（并轮换 refresh token）。"""
    return await refresh_access_token(data.refresh_token)
