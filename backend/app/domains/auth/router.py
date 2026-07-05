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
from app.core.deps import get_current_user, oauth2_scheme
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
    logout,
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
    """登录获取 token（OAuth2PasswordRequestForm: username=email, password）。

    P0-2：登录失败锁定。连续失败 ``login_max_failures`` 次后锁定
    ``login_lockout_minutes`` 分钟。锁定期间即使密码正确也拒绝登录。
    """
    from app.core.exceptions import AuthenticationError
    from app.core.login_lockout import check_lockout, record_failure, record_success

    # P0-2：锁定检查（先于密码校验，避免攻击者通过正确密码"试探"锁定状态）
    if await check_lockout(form.username):
        logger.warning(
            "login_locked",
            extra={"event": "login", "outcome": "locked", "email": form.username},
        )
        raise AuthenticationError("账号已锁定，请稍后重试")
    try:
        user = await authenticate_user(session, form.username, form.password)
    except AppError as exc:
        # 审计日志：登录失败可能表示凭据错误或账户停用，也可能是攻击者撞库。
        # form.username 即用户输入的 email（OAuth2PasswordRequestForm 约定）。
        # P0-2：记录失败次数（Redis 不可用时降级跳过）。
        await record_failure(form.username)
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
    # P0-2：登录成功清空失败计数
    await record_success(form.username)
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


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    user: User = Depends(get_current_user),
    token: str = Depends(oauth2_scheme),
) -> None:
    """P0-1：登出——把当前 access token 加入黑名单。

    需先通过 ``get_current_user`` 认证（确保 token 有效），再把其 jti 写入
    Redis 黑名单。后续请求携带同一 token 会被 ``verify_token_with_blacklist`` 拒绝。
    """
    await logout(token)
    logger.info(
        "logout_success",
        extra={"event": "logout", "outcome": "success", "user_id": str(user.id)},
    )
