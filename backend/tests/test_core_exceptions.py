"""core/exceptions.py 单元测试 — 异常层级与边缘情况。

覆盖：
- 各子类的 status_code 正确
- 各子类的 error_code 正确（对齐 `errors.spec.md`§4 命名）
- ``to_response`` 输出（detail 为 None 时省略）
- TokenExpiredError 继承 AuthenticationError 但 error_code 不同
- 字符串表示
"""

from __future__ import annotations

import pytest

from app.core.exceptions import (
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

# ===================== status_code =====================

@pytest.mark.parametrize(
    "exc_cls, expected_status",
    [
        (AppError, 500),
        (NotFoundError, 404),
        (ValidationError, 422),
        (AuthenticationError, 401),
        (TokenExpiredError, 401),
        (AuthorizationError, 403),
        (ConflictError, 409),
        (RateLimitError, 429),
        (LLMError, 502),
    ],
)
def test_app_error_status_code(exc_cls: type[AppError], expected_status: int) -> None:
    """各子类的 status_code 正确。"""
    exc = exc_cls("test error")
    assert exc.status_code == expected_status


# ===================== error_code（对齐 errors.spec.md§4） =====================

@pytest.mark.parametrize(
    "exc_cls, expected_code",
    [
        (AppError, "internal_error"),
        (NotFoundError, "not_found"),
        (ValidationError, "validation_error"),
        (AuthenticationError, "token_invalid"),
        (TokenExpiredError, "token_expired"),
        (AuthorizationError, "permission_denied"),
        (ConflictError, "conflict"),
        (RateLimitError, "rate_limited"),
        (LLMError, "llm_error"),
    ],
)
def test_app_error_error_code(exc_cls: type[AppError], expected_code: str) -> None:
    """各子类的 error_code 正确。"""
    exc = exc_cls("test error")
    assert exc.error_code == expected_code


# ===================== detail =====================

def test_app_error_with_detail() -> None:
    """带 detail 的错误。"""
    exc = NotFoundError("资源不存在", detail="agent_id=abc-123")
    assert exc.message == "资源不存在"
    assert exc.detail == "agent_id=abc-123"


def test_app_error_without_detail_defaults_none() -> None:
    """不带 detail 时默认为 None。"""
    exc = LLMError("llm 失败")
    assert exc.detail is None


# ===================== to_response（errors.spec.md§2/§5.1） =====================

def test_to_response_includes_detail_when_present() -> None:
    """detail 非 None 时 to_response 包含 detail 字段。"""
    exc = NotFoundError("资源不存在", detail="agent_id=abc-123")
    resp = exc.to_response()
    assert resp == {
        "error": "not_found",
        "message": "资源不存在",
        "detail": "agent_id=abc-123",
    }


def test_to_response_omits_detail_when_none() -> None:
    """detail 为 None 时 to_response 必须省略 detail 键，禁止返回 null。"""
    exc = LLMError("llm 失败")
    resp = exc.to_response()
    assert resp == {"error": "llm_error", "message": "llm 失败"}
    assert "detail" not in resp


def test_to_response_uses_overridden_message() -> None:
    """子类默认 message 可被构造参数覆盖。"""
    exc = NotFoundError()
    assert exc.to_response()["message"] == "资源不存在"
    exc2 = NotFoundError("agent 不存在")
    assert exc2.to_response()["message"] == "agent 不存在"


# ===================== 继承 =====================

def test_app_error_inheritance() -> None:
    """所有子类继承 AppError。"""
    for exc_cls in [
        NotFoundError,
        ValidationError,
        AuthenticationError,
        TokenExpiredError,
        AuthorizationError,
        ConflictError,
        RateLimitError,
        LLMError,
    ]:
        exc = exc_cls("msg")
        assert isinstance(exc, AppError)
        assert isinstance(exc, Exception)


def test_token_expired_caught_as_authentication_error() -> None:
    """TokenExpiredError 继承 AuthenticationError，可被父类 except 捕获。"""
    with pytest.raises(AuthenticationError):
        raise TokenExpiredError("expired")


# ===================== str/repr =====================

def test_app_error_str_repr() -> None:
    """字符串表示。"""
    exc = NotFoundError("找不到 Agent", detail="id=xxx")
    # str(exc) 应包含 message
    assert "找不到 Agent" in str(exc)
    # repr 应包含类名
    assert "NotFoundError" in repr(exc)


def test_app_error_raised_and_caught() -> None:
    """异常可被 raise/except 捕获。"""
    with pytest.raises(NotFoundError) as exc_info:
        raise NotFoundError("not here")
    assert exc_info.value.status_code == 404
    assert exc_info.value.error_code == "not_found"


def test_app_error_subclass_caught_as_base() -> None:
    """子类异常可被 AppError 基类捕获。"""
    with pytest.raises(AppError):
        raise ValidationError("bad input")
