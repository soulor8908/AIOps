"""密码哈希 — bcrypt（直接调用）。

仅负责明文 ↔ 哈希转换，不涉及 JWT 或 FastAPI 依赖（见 ``jwt.py`` / ``deps.py``）。

依赖：``bcrypt`` 直接调用。原 passlib 1.7.4 与 bcrypt >= 4.1 不兼容（passlib 读取
已删除的 ``__about__`` 属性），且 passlib 维护停滞。bcrypt 4.x 内置类型注解，
直接调用更轻量、更可控。

向后兼容：passlib 的 bcrypt 后端产出的哈希（``$2b$...``）与直接 bcrypt 调用产出
的哈希格式一致，``verify_password`` 可验证两种来源的哈希。
"""

from __future__ import annotations

import bcrypt

# bcrypt 算法对密码长度有 72 字节硬上限，超出部分会被静默截断。
# passlib 1.7.x 在内部自动截断；直接调用 bcrypt 时需显式处理以保持行为一致。
# 注意：按 UTF-8 编码后截断 72 字节，可能在多字节字符中间截断——这与 passlib
# 的行为一致（passlib 也是先 encode 再截断），不引入语义差异。
_MAX_PASSWORD_BYTES = 72


def _encode_password(plain: str) -> bytes:
    """密码 → UTF-8 bytes，截断至 72 字节（bcrypt 算法上限）。"""
    return plain.encode("utf-8")[:_MAX_PASSWORD_BYTES]


def hash_password(plain: str) -> str:
    """bcrypt 哈希明文密码。返回 ``$2b$...`` 字符串。"""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(_encode_password(plain), salt)
    return hashed.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希。

    哈希格式异常或类型错误时返回 False（不抛异常），与 passlib 行为一致——
    调用方仅关心"密码是否正确"，不关心哈希格式问题。
    """
    try:
        hashed_bytes = hashed.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        return False
    return bcrypt.checkpw(_encode_password(plain), hashed_bytes)


# P0-4：弱密码黑名单。这些是泄露数据库中最常见的密码，即使满足 8 字符长度
# 也应拒绝。按 OWLOS Top 10000 精简，覆盖最高频的弱密码。
_WEAK_PASSWORDS: frozenset[str] = frozenset({
    "password", "password1", "password12", "password123",
    "passw0rd", "passw0rd1", "passw0rd12",
    "12345678", "123456789", "1234567890", "12345678a",
    "11111111", "00000000", "88888888", "66666666",
    "qwerty123", "qwertyui", "qwerty12",
    "abc12345", "abcd1234", "abcdabcd",
    "iloveyou", "letmein1", "welcome1", "monkey123",
    "football", "baseball1", "dragon12",
    "admin123", "admin1234", "root1234", "test1234",
})


def validate_password_strength(plain: str) -> None:
    """P0-4：密码强度校验。拒绝过于简单的密码。

    规则（security.spec.md§6 最小长度 8 字符的增强）：
    1. 长度 ≥ 8（UserCreate 的 Pydantic Field 已强制，此处兜底）
    2. 拒绝纯数字密码（最易被字典攻击命中）
    3. 拒绝常见弱密码黑名单

    抛 ``ValueError``（Pydantic validator 约定），由 UserCreate 的 field_validator
    捕获转为 ValidationError(422)。
    """
    if len(plain) < 8:
        raise ValueError("密码长度至少 8 字符")
    if plain.isdigit():
        raise ValueError("密码不能为纯数字")
    if plain.lower() in _WEAK_PASSWORDS:
        raise ValueError("密码过于常见，请使用更强的密码")
