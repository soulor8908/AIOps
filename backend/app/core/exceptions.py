"""全局异常基类 ``AppError`` 与错误响应格式。

子类按领域分组在 ``app.core.errors`` 子包：
- ``errors.base``: AppError 基类 + ``to_response``
- ``errors.auth``: AuthenticationError / TokenExpiredError / AuthorizationError
- ``errors.resource``: NotFoundError / ConflictError / ValidationError
- ``errors.system``: RateLimitError / LLMError

此处通过 re-export 保持 ``from app.core.exceptions import NotFoundError`` 向后兼容。
错误码命名遵循 `specs/errors.spec.md`§4（snake_case）。
"""

from __future__ import annotations

from app.core.errors import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    LLMError,
    NotFoundError,
    RateLimitError,
    TokenExpiredError,
    ValidationError,
)

__all__ = [
    "AppError",
    "AuthenticationError",
    "AuthorizationError",
    "ConflictError",
    "LLMError",
    "NotFoundError",
    "RateLimitError",
    "TokenExpiredError",
    "ValidationError",
]
