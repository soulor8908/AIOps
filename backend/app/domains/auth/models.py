"""Auth — ORM 模型 + Pydantic schemas。

ORM: User（映射 init.sql 中的 users 表，复用，不新建表）
Schema: UserCreate / UserLogin / UserOut / Token / TokenData / RefreshRequest
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """用户。复用 init.sql 的 users 表；role 由 is_admin 派生。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    @property
    def role(self) -> Literal["admin", "user"]:
        """由 is_admin 派生角色。"""
        return "admin" if self.is_admin else "user"


# ===================== Pydantic Schemas =====================


class UserCreate(BaseModel):
    """注册入参。password 为明文，落库前经 security.hash_password 哈希。"""

    email: EmailStr
    username: str = Field(min_length=1, max_length=64)
    full_name: str | None = Field(default=None, max_length=128)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _validate_password_strength(cls, v: str) -> str:
        """P0-4：密码强度校验。拒绝纯数字 / 常见弱密码。"""
        from app.core.security import validate_password_strength

        validate_password_strength(v)
        return v


class UserLogin(BaseModel):
    """登录入参（JSON 形式；/auth/token 走 OAuth2PasswordRequestForm）。"""

    email: EmailStr
    password: str = Field(min_length=8)


class UserOut(BaseModel):
    """用户对外视图。role 由 is_admin 派生。"""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    email: str
    username: str
    full_name: str | None = None
    is_active: bool
    created_at: datetime
    role: Literal["admin", "user"] = "user"

    @classmethod
    def from_orm_user(cls, user: User) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            username=user.username,
            full_name=user.full_name,
            is_active=user.is_active,
            created_at=user.created_at,
            role=user.role,
        )


class TokenData(BaseModel):
    """JWT 解析后的载荷。"""

    sub: str | None = None  # user_id
    type: Literal["access", "refresh"] | None = None
    exp: int | None = None  # 过期时间戳


class Token(BaseModel):
    """登录 / 刷新返回的 token 包。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # access token 有效秒数


class RefreshRequest(BaseModel):
    """刷新请求体。"""

    refresh_token: str = Field(min_length=1)
