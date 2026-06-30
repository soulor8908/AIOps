"""密码哈希 — bcrypt。

仅负责明文 ↔ 哈希转换，不涉及 JWT 或 FastAPI 依赖（见 ``jwt.py`` / ``deps.py``）。
"""

from __future__ import annotations

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """bcrypt 哈希明文密码。"""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希。"""
    return pwd_context.verify(plain, hashed)
