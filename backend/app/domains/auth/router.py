"""Auth router — /api/v1/auth 端点。

| POST /auth/register | 注册 |
| POST /auth/token    | 登录（OAuth2PasswordRequestForm）+ set httpOnly cookie |
| GET  /auth/me       | 当前用户（cookie 或 Bearer） |
| POST /auth/refresh  | 刷新 token（refresh_token 取自 body 或 cookie）+ set cookie |
| POST /auth/logout   | 登出（撤销 + 清 cookie，不强制要求有效 token） |

Batch 6c：access/refresh token 同时以 httpOnly cookie 下发，前端不再持有
token 明文 → XSS 无法偷取。Authorization header 仍兼容（API 客户端 / scraper）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import _PROD_ENVS, settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.core.exceptions import AppError, AuthenticationError
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

# ===================== httpOnly cookie helpers =====================
#
# cookie 模式：access/refresh token 存 httpOnly cookie，JS 不可读 → 防 XSS 偷取。
# - samesite=lax：防 CSRF（跨站 POST 不带 cookie）
# - secure：仅生产启用（dev 走 http://localhost，secure=True 会导致 cookie 不下发）
# - path=/api/v1：限制 cookie 仅随 API 请求发送，不随静态资源请求泄露
_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"
_COOKIE_PATH = "/api/v1"


def _is_secure_cookie() -> bool:
    """生产环境（HTTPS）启用 secure，dev/test（HTTP）关闭否则 cookie 不下发。"""
    return settings.environment.lower() in _PROD_ENVS


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """将 access/refresh token 写入 httpOnly cookie。"""
    secure = _is_secure_cookie()
    response.set_cookie(
        _ACCESS_COOKIE,
        access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.access_token_expire_seconds,
        path=_COOKIE_PATH,
    )
    response.set_cookie(
        _REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.refresh_token_expire_seconds,
        path=_COOKIE_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    """清除 auth cookie（path 必须与 set 时一致才能正确删除）。"""
    response.delete_cookie(_ACCESS_COOKIE, path=_COOKIE_PATH)
    response.delete_cookie(_REFRESH_COOKIE, path=_COOKIE_PATH)


def _extract_token(request: Request) -> str | None:
    """从 cookie 或 Authorization header 提取 access token（logout 用，不抛错）。"""
    token = request.cookies.get(_ACCESS_COOKIE)
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


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
    response: Response,  # FastAPI 自动注入当前请求的 Response 实例
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> Token:
    """登录获取 token（OAuth2PasswordRequestForm: username=email, password）。

    P0-2：登录失败锁定。连续失败 ``login_max_failers`` 次后锁定
    ``login_lockout_minutes`` 分钟。锁定期间即使密码正确也拒绝登录。

    Batch 6c：成功时通过 httpOnly cookie 下发 access/refresh token，前端不再
    持有 token 明文。响应体仍返回 Token（API 客户端兼容）。
    """
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
    token = issue_token_pair(user)
    # Batch 6c：httpOnly cookie 下发 token（前端 cookie 模式）
    _set_auth_cookies(response, token.access_token, token.refresh_token)
    logger.info(
        "login_success",
        extra={
            "event": "login",
            "outcome": "success",
            "user_id": str(user.id),
            "email": user.email,
        },
    )
    return token


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    """获取当前登录用户信息。"""
    return to_user_out(user)


@router.post("/refresh", response_model=Token)
async def refresh(
    request: Request,
    response: Response,
    data: RefreshRequest | None = None,
) -> Token:
    """使用 refresh token 换取新的 access token（并轮换 refresh token）。

    Batch 6c：refresh_token 优先取自 body（API 客户端），回退 httpOnly cookie
    （前端 cookie 模式）。成功后重新 set cookie。
    """
    refresh_token_str: str | None = None
    if data is not None and data.refresh_token:
        refresh_token_str = data.refresh_token
    else:
        refresh_token_str = request.cookies.get(_REFRESH_COOKIE)
    if not refresh_token_str:
        raise AuthenticationError("未提供 refresh token")
    token = await refresh_access_token(refresh_token_str)
    # Batch 6c：轮换后的新 token 写入 cookie（覆盖旧值）
    _set_auth_cookies(response, token.access_token, token.refresh_token)
    return token


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    request: Request,
    response: Response,
) -> None:
    """P0-1 + Batch 6c：登出——撤销 access token + 清除 httpOnly cookie。

    不强制要求有效 token（access token 可能已过期，此时仍需能登出清 cookie）。
    尽力从 cookie/header 提取 token 撤销其 jti，失败也清除 cookie，确保前端
    总能成功登出。原 ``Depends(get_current_user)`` 在 token 过期时会 401 拒绝
    登出请求，导致 cookie 残留 → 此处改为无认证依赖。
    """
    token = _extract_token(request)
    if token:
        try:
            await logout(token)
        except AppError:
            # token 过期/无效——jti 撤销失败，但仍清除 cookie
            logger.warning("logout_revoke_failed", exc_info=True)
    _clear_auth_cookies(response)
    logger.info("logout_success", extra={"event": "logout", "outcome": "success"})
