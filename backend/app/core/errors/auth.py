"""认证与授权相关异常。"""

from __future__ import annotations

from app.core.errors.base import AppError


class AuthenticationError(AppError):
    """未认证 (401) — token 缺失或无效。"""

    status_code = 401
    error_code = "token_invalid"
    message = "认证凭据无效"


class TokenExpiredError(AuthenticationError):
    """token 已过期 (401)。

    继承 ``AuthenticationError`` 以兼容 ``except AuthenticationError`` 捕获，
    但 ``error_code`` 单独标记为 ``token_expired``。
    """

    error_code = "token_expired"
    message = "认证凭据已过期"


class AuthorizationError(AppError):
    """无权限 (403)。"""

    status_code = 403
    error_code = "permission_denied"
    message = "无权访问该资源"
