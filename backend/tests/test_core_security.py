"""core/security.py 单元测试 — JWT 签发/校验 + 密码哈希 + token 类型区分。

覆盖：
- create_access_token / create_refresh_token: 签发 JWT、payload 包含 sub/exp/type
- verify_token / decode_token: 有效/过期/无效/错误类型 token
- hash_password / verify_password: bcrypt 哈希与校验
- token_expired 错误码单独标记（遵循 auth/SPEC.md§Error Cases）
"""

from __future__ import annotations

import time

import pytest
from jose import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError, TokenExpiredError
from app.core.security import (
    ALGORITHM,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
    verify_token,
)

# ===================== create_access_token =====================

def test_create_access_token_returns_valid_jwt() -> None:
    """创建 token 并验证可解码。"""
    token = create_access_token("user-123")
    payload = jwt.decode(token, settings.effective_secret_key, algorithms=[ALGORITHM])
    assert payload["sub"] == "user-123"
    assert payload["type"] == TOKEN_TYPE_ACCESS


def test_create_access_token_contains_sub_and_exp() -> None:
    """验证 token payload 包含 sub / exp / type。"""
    token = create_access_token("user-456")
    payload = jwt.decode(token, settings.effective_secret_key, algorithms=[ALGORITHM])
    assert "sub" in payload
    assert "exp" in payload
    assert payload["sub"] == "user-456"
    assert payload["type"] == TOKEN_TYPE_ACCESS
    assert isinstance(payload["exp"], int)
    # exp 应该在未来
    assert payload["exp"] > int(time.time())


def test_create_access_token_with_custom_expiry() -> None:
    """自定义过期秒数。"""
    token_short = create_access_token("user-a", expires_seconds=60)
    token_long = create_access_token("user-b", expires_seconds=36000)

    payload_short = jwt.decode(token_short, settings.effective_secret_key, algorithms=[ALGORITHM])
    payload_long = jwt.decode(token_long, settings.effective_secret_key, algorithms=[ALGORITHM])

    # 60s vs 36000s，exp 差距应大于 30000s
    diff = payload_long["exp"] - payload_short["exp"]
    assert diff > 30000


# ===================== create_refresh_token =====================

def test_create_refresh_token_has_refresh_type() -> None:
    """refresh token 的 type claim 必须为 refresh。"""
    token = create_refresh_token("user-r")
    payload = jwt.decode(token, settings.effective_secret_key, algorithms=[ALGORITHM])
    assert payload["sub"] == "user-r"
    assert payload["type"] == TOKEN_TYPE_REFRESH


def test_refresh_token_lives_longer_than_access() -> None:
    """refresh token 默认过期（7d）应远晚于 access token（24h）。"""
    access = create_access_token("user-x")
    refresh = create_refresh_token("user-x")
    pa = jwt.decode(access, settings.effective_secret_key, algorithms=[ALGORITHM])
    pr = jwt.decode(refresh, settings.effective_secret_key, algorithms=[ALGORITHM])
    assert pr["exp"] > pa["exp"]


# ===================== verify_token / decode_token =====================

def test_verify_token_valid() -> None:
    """验证有效 token。"""
    token = create_access_token("user-valid")
    sub = verify_token(token)
    assert sub == "user-valid"


def test_verify_token_rejects_refresh_type() -> None:
    """verify_token 应拒绝 refresh token（端点不应接受 refresh 类型）。"""
    refresh = create_refresh_token("user-r")
    with pytest.raises(AuthenticationError):
        verify_token(refresh)


def test_verify_token_expired_raises_token_expired() -> None:
    """过期 token 单独抛 TokenExpiredError（error_code=token_expired）。"""
    # 创建一个已过期的 token（expires_seconds=-60 使 exp 在过去）
    token = create_access_token("user-expired", expires_seconds=-60)
    with pytest.raises(TokenExpiredError) as exc_info:
        verify_token(token)
    assert exc_info.value.error_code == "token_expired"
    assert exc_info.value.status_code == 401


def test_decode_token_invalid_raises_authentication_error() -> None:
    """无效 token 抛 AuthenticationError（error_code=token_invalid）。"""
    with pytest.raises(AuthenticationError) as exc_info:
        decode_token("not.a.valid.jwt.token")
    assert exc_info.value.error_code == "token_invalid"

    with pytest.raises(AuthenticationError):
        decode_token("")


def test_verify_token_invalid() -> None:
    """验证无效 token 抛 AuthenticationError。"""
    with pytest.raises(AuthenticationError):
        verify_token("not.a.valid.jwt.token")

    with pytest.raises(AuthenticationError):
        verify_token("")


def test_verify_token_wrong_secret() -> None:
    """用错误密钥签发的 token 应被拒绝。"""
    token = jwt.encode(
        {"sub": "user-x", "exp": int(time.time()) + 3600, "type": TOKEN_TYPE_ACCESS},
        "wrong-secret",
        algorithm=ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        verify_token(token)


def test_verify_token_missing_sub() -> None:
    """token 缺少 sub 字段时抛异常。"""
    token = jwt.encode(
        {"exp": int(time.time()) + 3600, "type": TOKEN_TYPE_ACCESS},
        settings.effective_secret_key,
        algorithm=ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        verify_token(token)


# ===================== hash_password =====================

def test_hash_password_returns_different_hash() -> None:
    """密码哈希不等于原文。"""
    plain = "MySecret123!"
    hashed = hash_password(plain)
    assert hashed != plain
    assert len(hashed) > 0


def test_hash_password_different_each_time() -> None:
    """同一密码两次哈希结果不同（bcrypt salt）。"""
    plain = "SamePassword456"
    hash1 = hash_password(plain)
    hash2 = hash_password(plain)
    assert hash1 != hash2  # bcrypt 每次使用不同 salt


# ===================== verify_password =====================

def test_verify_password_correct() -> None:
    """正确密码验证通过。"""
    plain = "CorrectPass789"
    hashed = hash_password(plain)
    assert verify_password(plain, hashed) is True


def test_verify_password_wrong() -> None:
    """错误密码验证失败。"""
    plain = "CorrectPass000"
    hashed = hash_password(plain)
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("", hashed) is False
