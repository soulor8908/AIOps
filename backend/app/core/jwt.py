"""JWT 编解码与签发 — access / refresh 双 token。

遵循 `auth/SPEC.md`§Auth Dependencies 与 `specs/security.spec.md`§2：
- access token 默认 24h，refresh token 默认 7d（可配置）
- ``type`` claim 区分 access / refresh，``verify_token`` 拒绝 refresh 类型
- 过期单独抛 ``TokenExpiredError``，其它无效抛 ``AuthenticationError``

依赖：``PyJWT``（活跃维护；python-jose 自 2022 年停止维护后迁移至此）。
PyJWT 2.x ``encode`` 直接返回 str，``decode`` 返回 dict，API 简洁。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt as pyjwt
from jwt import ExpiredSignatureError, PyJWTError

from app.core.config import settings
from app.core.exceptions import AuthenticationError, TokenExpiredError

ALGORITHM: Literal["HS256"] = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def _encode(payload: dict[str, object]) -> str:
    # PyJWT 2.x encode 返回 str（1.x 返回 bytes），无需 .decode()。
    return pyjwt.encode(payload, settings.effective_secret_key, algorithm=ALGORITHM)


def create_access_token(subject: str, expires_seconds: int | None = None) -> str:
    """签发 access token。subject 通常是 user_id 字符串。"""
    expire = datetime.now(UTC) + timedelta(
        seconds=expires_seconds or settings.access_token_expire_seconds
    )
    return _encode({"sub": subject, "exp": expire, "type": TOKEN_TYPE_ACCESS})


def create_refresh_token(subject: str, expires_seconds: int | None = None) -> str:
    """签发 refresh token（默认 7d，可配置 ``REFRESH_TOKEN_EXPIRE_DAYS``）。"""
    expire = datetime.now(UTC) + timedelta(
        seconds=expires_seconds or settings.refresh_token_expire_seconds
    )
    return _encode({"sub": subject, "exp": expire, "type": TOKEN_TYPE_REFRESH})


def decode_token(token: str) -> dict[str, object]:
    """校验并解析 JWT，返回完整 payload。

    - 过期 → ``TokenExpiredError``（``error_code="token_expired"``）
    - 其它无效 → ``AuthenticationError``（``error_code="token_invalid"``）
    """
    try:
        # PyJWT 自动校验 exp（默认）。返回类型为 dict[str, Any]，此处窄化为业务类型。
        payload: dict[str, object] = pyjwt.decode(
            token, settings.effective_secret_key, algorithms=[ALGORITHM]
        )
        return payload
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("认证凭据已过期") from exc
    except PyJWTError as exc:
        # PyJWTError 覆盖所有解码失败：签名错误、格式错误、claim 校验失败等。
        raise AuthenticationError(f"认证凭据无效: {exc}") from exc


def verify_token(token: str) -> str:
    """校验 access token 并返回 subject（user_id）。拒绝 refresh token 类型。"""
    payload = decode_token(token)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AuthenticationError("token 缺少 sub")
    if payload.get("type") == TOKEN_TYPE_REFRESH:
        raise AuthenticationError("token 类型错误")
    return sub
