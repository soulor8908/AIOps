"""JWT 编解码与签发 — access / refresh 双 token。

遵循 `auth/SPEC.md`§Auth Dependencies 与 `specs/security.spec.md`§2：
- access token 默认 24h，refresh token 默认 7d（可配置）
- ``type`` claim 区分 access / refresh，``verify_token`` 拒绝 refresh 类型
- 过期单独抛 ``TokenExpiredError``，其它无效抛 ``AuthenticationError``
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError, TokenExpiredError

ALGORITHM: Literal["HS256"] = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def _encode(payload: dict[str, object]) -> str:
    return jwt.encode(payload, settings.effective_secret_key, algorithm=ALGORITHM)


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
        return jwt.decode(token, settings.effective_secret_key, algorithms=[ALGORITHM])
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("认证凭据已过期") from exc
    except JWTError as exc:
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
