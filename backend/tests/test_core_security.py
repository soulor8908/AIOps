"""core/security.py 单元测试 — JWT 签发/校验 + 密码哈希。

覆盖：
- create_access_token: 签发 JWT、payload 包含 sub/exp、自定义过期时间
- verify_token: 有效/过期/无效 token
- hash_password / verify_password: bcrypt 哈希与校验
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from jose import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    ALGORITHM,
    create_access_token,
    hash_password,
    verify_password,
    verify_token,
)


# ===================== create_access_token =====================

def test_create_access_token_returns_valid_jwt() -> None:
    """创建 token 并验证可解码。"""
    token = create_access_token("user-123")
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    assert payload["sub"] == "user-123"


def test_create_access_token_contains_sub_and_exp() -> None:
    """验证 token payload 包含 sub 和 exp。"""
    token = create_access_token("user-456")
    payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    assert "sub" in payload
    assert "exp" in payload
    assert payload["sub"] == "user-456"
    assert isinstance(payload["exp"], int)
    # exp 应该在未来
    assert payload["exp"] > int(time.time())


def test_create_access_token_with_custom_expiry() -> None:
    """自定义过期时间。"""
    token_short = create_access_token("user-a", expires_minutes=1)
    token_long = create_access_token("user-b", expires_minutes=600)

    payload_short = jwt.decode(token_short, settings.secret_key, algorithms=[ALGORITHM])
    payload_long = jwt.decode(token_long, settings.secret_key, algorithms=[ALGORITHM])

    # 1 分钟过期 vs 600 分钟过期，exp 差距应大于 500 分钟
    diff = payload_long["exp"] - payload_short["exp"]
    assert diff > 500 * 60


# ===================== verify_token =====================

def test_verify_token_valid() -> None:
    """验证有效 token。"""
    token = create_access_token("user-valid")
    sub = verify_token(token)
    assert sub == "user-valid"


def test_verify_token_expired() -> None:
    """验证过期 token 抛异常。"""
    # 创建一个已过期的 token（expires_minutes=-1 使 exp 在过去）
    token = create_access_token("user-expired", expires_minutes=-1)
    # jose 允许签发过期 token，但 decode 默认验证 exp
    with pytest.raises(AuthenticationError):
        verify_token(token)


def test_verify_token_invalid() -> None:
    """验证无效 token 抛异常。"""
    with pytest.raises(AuthenticationError):
        verify_token("not.a.valid.jwt.token")

    with pytest.raises(AuthenticationError):
        verify_token("")


def test_verify_token_wrong_secret() -> None:
    """用错误密钥签发的 token 应被拒绝。"""
    token = jwt.encode(
        {"sub": "user-x", "exp": int(time.time()) + 3600},
        "wrong-secret",
        algorithm=ALGORITHM,
    )
    with pytest.raises(AuthenticationError):
        verify_token(token)


def test_verify_token_missing_sub() -> None:
    """token 缺少 sub 字段时抛异常。"""
    token = jwt.encode(
        {"exp": int(time.time()) + 3600},
        settings.secret_key,
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
