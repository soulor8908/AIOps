"""``AppError`` 基类 — 所有应用异常的根。

独立于子类定义，避免与 ``exceptions.py`` re-export 形成循环导入。
``to_response`` 遵循 `specs/errors.spec.md`§2：``detail`` 为 None 时省略。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有应用异常基类。

    子类通过类属性覆盖 ``status_code`` / ``error_code`` / 默认 ``message``，
    构造时可传 ``message`` / ``detail`` 覆盖。
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
