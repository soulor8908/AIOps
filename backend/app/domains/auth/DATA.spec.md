# Data Spec: Auth

> 用户表复用 `backend/init.sql` 中的 `users` 表。ORM 映射该表，`role` 由 `is_admin` 派生（不新增 `role` 列）。
> 依赖扩展：`citext`（email 大小写不敏感）、`pgcrypto`（`gen_random_uuid()`），见 `init.sql` 顶部。

## users 表 DDL（与 init.sql 一致，勿改动）

```sql
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         CITEXT UNIQUE NOT NULL,
    username      VARCHAR(64) UNIQUE NOT NULL,
    full_name     VARCHAR(128),
    hashed_password VARCHAR(255) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 字段说明
| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | UUID | PK, default `gen_random_uuid()` | 用户主键，即 JWT `sub` |
| email | CITEXT | UNIQUE NOT NULL | 登录账号，大小写不敏感 |
| username | VARCHAR(64) | UNIQUE NOT NULL | 展示名 |
| full_name | VARCHAR(128) | NULLable | 真实姓名 |
| hashed_password | VARCHAR(255) | NOT NULL | bcrypt 哈希（含 salt / 版本前缀） |
| is_active | BOOLEAN | NOT NULL DEFAULT TRUE | 账号启用状态 |
| is_admin | BOOLEAN | NOT NULL DEFAULT FALSE | 是否管理员；派生 `role` |
| created_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | 创建时间 |
| updated_at | TIMESTAMPTZ | NOT NULL DEFAULT now() | 更新时间 |

## role 派生规则
`role` 不落库，由 `is_admin` 派生：
- `is_admin = true  → role = "admin"`
- `is_admin = false → role = "user"`

## ORM 模型（SQLAlchemy 2.0，Mapped 风格）

```python
"""Auth — ORM 模型 + Pydantic schemas。

ORM: User（映射 init.sql 中的 users 表，复用，不新建表）
Schema: UserCreate / UserLogin / UserOut / Token / TokenData
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class User(Base):
    """用户。复用 init.sql 的 users 表；role 由 is_admin 派生。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    @property
    def role(self) -> Literal["admin", "user"]:
        """由 is_admin 派生角色。"""
        return "admin" if self.is_admin else "user"
```

## 索引策略
- `email`：`UNIQUE` 约束自动建唯一索引（CITEXT 大小写不敏感），保证注册 email 唯一、登录按 email 查询走索引
- `username`：`UNIQUE` 唯一索引
- `id`：主键索引
- 无需额外复合索引（alpha 期查询均按 `email` / `id` 单列）

## Pydantic Schemas

```python
class UserCreate(BaseModel):
    """注册入参。password 为明文，落库前经 security.hash_password 哈希。"""
    email: EmailStr
    username: str = Field(min_length=1, max_length=64)
    full_name: str | None = Field(default=None, max_length=128)
    password: str = Field(min_length=8, max_length=128)


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
    def from_orm_user(cls, user: User) -> "UserOut":
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
    sub: str | None = None                 # user_id
    type: Literal["access", "refresh"] | None = None
    exp: int | None = None                 # 过期时间戳


class Token(BaseModel):
    """登录 / 刷新返回的 token 包。"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int                        # access token 有效秒数


class RefreshRequest(BaseModel):
    """刷新请求体。"""
    refresh_token: str = Field(min_length=1)
```

## 哈希与 token 约定
- 密码：`security.hash_password(plain)` → bcrypt hash；`security.verify_password(plain, hashed)` 校验
- access token：`{sub: user_id, exp, type:"access"}`，HS256，默认 24h（`ACCESS_TOKEN_EXPIRE_MINUTES=1440`）
- refresh token：`{sub: user_id, exp, type:"refresh"}`，HS256，默认 7d（`REFRESH_TOKEN_EXPIRE_DAYS=7`），刷新时轮换
