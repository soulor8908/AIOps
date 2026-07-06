"""异常子包 — 按领域分组的 ``AppError`` 子类。

- ``base``: AppError 基类 + ``to_response``
- ``auth``: AuthenticationError / TokenExpiredError / AuthorizationError
- ``resource``: NotFoundError / ConflictError / ValidationError
- ``system``: RateLimitError / LLMError / EmbeddingError / GatewayTimeoutError

``app.core.exceptions`` 通过 re-export 保持向后兼容。
"""

from __future__ import annotations

from app.core.errors.auth import AuthenticationError, AuthorizationError, TokenExpiredError
from app.core.errors.base import AppError
from app.core.errors.resource import ConflictError, NotFoundError, ValidationError
from app.core.errors.system import EmbeddingError, GatewayTimeoutError, LLMError, RateLimitError

__all__ = [
    "AppError",
    "AuthenticationError",
    "AuthorizationError",
    "ConflictError",
    "EmbeddingError",
    "GatewayTimeoutError",
    "LLMError",
    "NotFoundError",
    "RateLimitError",
    "TokenExpiredError",
    "ValidationError",
]
