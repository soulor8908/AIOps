"""资源相关异常 — 不存在 / 冲突 / 校验失败。"""

from __future__ import annotations

from app.core.errors.base import AppError


class NotFoundError(AppError):
    """资源不存在 (404)。"""

    status_code = 404
    error_code = "not_found"
    message = "资源不存在"


class ValidationError(AppError):
    """输入校验失败 (422)。"""

    status_code = 422
    error_code = "validation_error"
    message = "输入校验失败"


class ConflictError(AppError):
    """资源冲突 (409)。"""

    status_code = 409
    error_code = "conflict"
    message = "资源冲突"
