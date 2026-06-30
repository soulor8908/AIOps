"""全局异常层级。

所有领域 service 抛出本模块异常，main.py 注册统一处理器。
每个异常携带 HTTP status_code 与 error_code，便于 FastAPI 处理器映射。

错误码命名遵循 `specs/errors.spec.md`§4（snake_case，全小写）：
- `not_found` / `validation_error` / `token_invalid` / `token_expired`
- `permission_denied` / `conflict` / `rate_limited` / `llm_error` / `internal_error`
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有应用异常基类。

    子类通过类属性覆盖 ``status_code`` / ``error_code`` / 默认 ``message``，
    构造时可传 ``message`` / ``detail`` 覆盖。``to_response`` 输出遵循
    `errors.spec.md`§2：``detail`` 为 None 时省略该字段，禁止返回 ``null``。
    """

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "服务器内部错误"

    def __init__(self, message: str | None = None, detail: Any = None) -> None:
        super().__init__(message if message is not None else self.message)
        if message is not None:
            self.message = message
        self.detail = detail

    def to_response(self) -> dict[str, Any]:
        """转换为统一错误响应体。``detail`` 为 None 时省略。"""
        resp: dict[str, Any] = {"error": self.error_code, "message": self.message}
        if self.detail is not None:
            resp["detail"] = self.detail
        return resp


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


class AuthenticationError(AppError):
    """未认证 (401) — token 缺失或无效。"""

    status_code = 401
    error_code = "token_invalid"
    message = "认证凭据无效"


class TokenExpiredError(AuthenticationError):
    """token 已过期 (401)。

    继承 ``AuthenticationError`` 以兼容 ``except AuthenticationError`` 捕获，
    但 ``error_code`` 单独标记为 ``token_expired``，遵循 `errors.spec.md`§4
    与 `auth/SPEC.md`§Auth Dependencies。
    """

    error_code = "token_expired"
    message = "认证凭据已过期"


class AuthorizationError(AppError):
    """无权限 (403)。"""

    status_code = 403
    error_code = "permission_denied"
    message = "无权访问该资源"


class ConflictError(AppError):
    """资源冲突 (409)。"""

    status_code = 409
    error_code = "conflict"
    message = "资源冲突"


class RateLimitError(AppError):
    """触发限流 (429)。"""

    status_code = 429
    error_code = "rate_limited"
    message = "请求过于频繁，请稍后重试"


class LLMError(AppError):
    """LLM 调用失败 (502)。"""

    status_code = 502
    error_code = "llm_error"
    message = "LLM 调用失败"
