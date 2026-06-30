"""全局异常层级。

所有领域 service 抛出本模块异常，main.py 注册统一处理器。
每个异常携带 HTTP status_code，便于 FastAPI 处理器映射。
"""

from __future__ import annotations


class AppError(Exception):
    """所有应用异常基类。"""

    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(AppError):
    """资源不存在 (404)。"""

    status_code = 404
    error_code = "not_found"


class ValidationError(AppError):
    """输入校验失败 (422)。"""

    status_code = 422
    error_code = "validation_error"


class AuthenticationError(AppError):
    """未认证 (401)。"""

    status_code = 401
    error_code = "authentication_error"


class AuthorizationError(AppError):
    """无权限 (403)。"""

    status_code = 403
    error_code = "authorization_error"


class ConflictError(AppError):
    """资源冲突 (409)。"""

    status_code = 409
    error_code = "conflict"


class LLMError(AppError):
    """LLM 调用失败 (502)。"""

    status_code = 502
    error_code = "llm_error"
