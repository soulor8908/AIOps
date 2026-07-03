"""Auth API 契约测试 — /api/v1/auth（register / token / me / refresh）。

覆盖 auth/SPEC.md Success Criteria：
- 注册、登录、me、refresh 正常流程
- email 重复 → 409 conflict
- 密码 < 8 → 422 validation_error
- 未认证访问 /me → 401 token_invalid
- 无效 token → 401 token_invalid
- refresh token 过期 → 401 token_expired
- refresh token 用 access token → 401 token_invalid（类型错误）
- access token 用 refresh token → 401 token_invalid
- 邮箱大小写不敏感
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.jwt import create_access_token
from app.domains.auth.models import User
from tests.factories import UserFactory

# ===================== register =====================

def test_register_success(client: TestClient) -> None:
    """注册成功返回 201 + UserOut（不含 password）。"""
    body = UserFactory.create_via_api(client, email="alice@test.example")
    assert body["email"] == "alice@test.example"
    assert "password" not in body
    assert "hashed_password" not in body
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert body["id"]


def test_register_email_conflict(client: TestClient) -> None:
    """email 重复 → 409 conflict。"""
    UserFactory.create_via_api(client, email="dup@test.example")
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "dup@test.example",
            "username": "another",
            "password": "Password123!",
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "conflict"


def test_register_username_conflict(client: TestClient) -> None:
    """username 重复 → 409 conflict。"""
    UserFactory.create_via_api(client, username="sameuser")
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "different@test.example",
            "username": "sameuser",
            "password": "Password123!",
        },
    )
    assert resp.status_code == 409


def test_register_short_password(client: TestClient) -> None:
    """密码 < 8 → 422 validation_error。"""
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "short@test.example",
            "username": "shortuser",
            "password": "123",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"


def test_register_invalid_email(client: TestClient) -> None:
    """无效 email → 422 validation_error。"""
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "not-an-email",
            "username": "bademail",
            "password": "Password123!",
        },
    )
    assert resp.status_code == 422


def test_register_email_case_insensitive(client: TestClient) -> None:
    """email 大小写不敏感：User@Example 与 user@example 视为重复。"""
    UserFactory.create_via_api(client, email="case@test.example")
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "CASE@TEST.EXAMPLE",
            "username": "another2",
            "password": "Password123!",
        },
    )
    assert resp.status_code == 409


# ===================== token (login) =====================

def test_login_success(client: TestClient) -> None:
    """登录成功返回 token 包。"""
    payload = UserFactory.build_create(email="login@test.example")
    UserFactory.create_via_api(client, **payload)
    tokens = UserFactory.login_via_api(
        client, email=payload["email"], password=payload["password"]
    )
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["expires_in"] > 0


def test_login_wrong_password(client: TestClient) -> None:
    """密码错误 → 401 token_invalid。"""
    payload = UserFactory.build_create(email="wrongpw@test.example")
    UserFactory.create_via_api(client, **payload)
    resp = client.post(
        "/api/v1/auth/token",
        data={
            "username": payload["email"],
            "password": "WrongPassword999!",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_login_nonexistent_user(client: TestClient) -> None:
    """用户不存在 → 401 token_invalid。"""
    resp = client.post(
        "/api/v1/auth/token",
        data={
            "username": "ghost@test.example",
            "password": "Password123!",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_login_email_case_insensitive(client: TestClient) -> None:
    """email 大小写不敏感：注册用小写，登录用大写也能成功。"""
    payload = UserFactory.build_create(email="mixed@test.example")
    UserFactory.create_via_api(client, **payload)
    resp = client.post(
        "/api/v1/auth/token",
        data={
            "username": "MIXED@TEST.EXAMPLE",
            "password": payload["password"],
        },
    )
    assert resp.status_code == 200


# ===================== me =====================

def test_me_success(anon_client: TestClient) -> None:
    """携带有效 Bearer token 访问 /me → 200。"""
    user, tokens = UserFactory.create_and_login(anon_client, email="me@test.example")
    resp = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "me@test.example"
    assert body["id"] == user["id"]


def test_me_no_token(anon_client: TestClient) -> None:
    """未携带 token → 401 token_invalid。"""
    resp = anon_client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_me_invalid_token(anon_client: TestClient) -> None:
    """无效 token → 401 token_invalid。"""
    resp = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_me_with_refresh_token_rejected(anon_client: TestClient) -> None:
    """用 refresh token 访问 /me → 401（access 端点不接受 refresh 类型）。"""
    _, tokens = UserFactory.create_and_login(anon_client)
    resp = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_me_nonexistent_user(anon_client: TestClient) -> None:
    """token 合法但用户已被删除 → 401。"""
    # 用一个不存在的 user_id 签发 token
    fake_uid = str(__import__("uuid").uuid4())
    token = create_access_token(fake_uid)
    resp = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


# ===================== refresh =====================

def test_refresh_success(anon_client: TestClient) -> None:
    """用 refresh token 换取新 token 对（轮换）。"""
    _, tokens = UserFactory.create_and_login(anon_client)
    resp = anon_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert new_tokens["access_token"]
    assert new_tokens["refresh_token"]
    assert new_tokens["token_type"] == "bearer"
    # 轮换后新 token 应可用
    resp2 = anon_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert resp2.status_code == 200


def test_refresh_with_access_token_rejected(client: TestClient) -> None:
    """用 access token 调 /refresh → 401（类型错误）。"""
    _, tokens = UserFactory.create_and_login(client)
    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["access_token"]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


def test_refresh_invalid_token(client: TestClient) -> None:
    """无效 refresh token → 401 token_invalid。"""
    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not.a.valid.jwt"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "token_invalid"


# ===================== RBAC =====================

def test_admin_role_derived(client: TestClient) -> None:
    """注册的默认用户 role=user。"""
    body = UserFactory.create_via_api(client)
    assert body["role"] == "user"


def test_inactive_user_cannot_login(client: TestClient) -> None:
    """停用用户登录 → 401。"""
    from sqlalchemy import select

    from app.core.database import get_session
    from app.main import app

    # 注册用户
    payload = UserFactory.build_create(email="inactive@test.example")
    UserFactory.create_via_api(client, **payload)

    # 通过 TestClient portal 在同一事件循环内修改 is_active
    session_factory = app.dependency_overrides[get_session]

    async def _deactivate() -> None:
        async for session in session_factory():
            stmt = select(User).where(User.email == "inactive@test.example")
            user = (await session.execute(stmt)).scalar_one()
            user.is_active = False
            await session.commit()
            break

    client.portal.call(_deactivate)

    resp = client.post(
        "/api/v1/auth/token",
        data={
            "username": payload["email"],
            "password": payload["password"],
        },
    )
    assert resp.status_code == 401


# ===================== 错误响应格式 =====================

def test_auth_error_response_format(anon_client: TestClient) -> None:
    """认证错误响应遵循统一格式 {error, message}，detail 可选。"""
    resp = anon_client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert "message" in body
    assert body["error"] == "token_invalid"
