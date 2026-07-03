"""RBAC 边界回归测试 — security.spec.md§3.2 + auth/SPEC.md§60-62。

覆盖三层边界：
1. 401 — 无 token 访问受保护端点（``anon_client``）
2. 403 — 普通用户（is_admin=False）访问 admin 端点（``user_client``）
3. 200 — 普通用户访问 user 级端点（``user_client``，正向校验）

FastAPI ``Depends`` 在 body 校验前解析，故 POST 端点以空 body 即可触发 401/403，
不会先返回 422。auth 依赖先于资源查找，故 PUT/DELETE 以不存在的 UUID 仍返回 403。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

# ===================== 401：无 token 访问受保护端点 =====================

# (method, path, json_body) — 覆盖所有 domain 的代表性端点
_PROTECTED_401 = [
    # prompts
    ("GET", "/api/v1/prompts", None),
    ("POST", "/api/v1/prompts", {}),
    # agents / workflows
    ("GET", "/api/v1/agents", None),
    ("POST", "/api/v1/agents", {}),
    ("GET", "/api/v1/workflows", None),
    ("POST", "/api/v1/workflows", {}),
    # models
    ("GET", "/api/v1/models", None),
    ("POST", "/api/v1/models", {}),
    # knowledge-bases
    ("GET", "/api/v1/knowledge-bases", None),
    ("POST", "/api/v1/knowledge-bases", {}),
    # analytics
    ("GET", "/api/v1/analytics/dashboard", None),
    ("GET", "/api/v1/analytics/conversations", None),
    # evals
    ("GET", "/api/v1/evals", None),
    ("POST", "/api/v1/evals", {}),
]


@pytest.mark.parametrize("method, path, json_body", _PROTECTED_401)
def test_no_token_returns_401(
    anon_client: TestClient, method: str, path: str, json_body: dict | None
) -> None:
    """无 Bearer token 访问受保护端点 → 401 token_invalid。"""
    kwargs: dict = {"json": json_body} if json_body is not None else {}
    resp = getattr(anon_client, method.lower())(path, **kwargs)
    assert resp.status_code == 401, (
        f"{method} {path}: expected 401, got {resp.status_code} — {resp.text[:200]}"
    )
    assert resp.json()["error"] == "token_invalid"


# ===================== 403：普通用户访问 admin 端点 =====================

# admin-only POST 端点（无需预存资源）
_ADMIN_CREATE_403 = [
    ("POST", "/api/v1/prompts", {}),
    ("POST", "/api/v1/agents", {}),
    ("POST", "/api/v1/workflows", {}),
    ("POST", "/api/v1/models", {}),
    ("POST", "/api/v1/knowledge-bases", {}),
]


@pytest.mark.parametrize("method, path, json_body", _ADMIN_CREATE_403)
def test_non_admin_create_returns_403(
    user_client: TestClient, method: str, path: str, json_body: dict | None
) -> None:
    """普通用户访问 admin-only 创建端点 → 403 permission_denied。"""
    kwargs: dict = {"json": json_body} if json_body is not None else {}
    resp = getattr(user_client, method.lower())(path, **kwargs)
    assert resp.status_code == 403, (
        f"{method} {path}: expected 403, got {resp.status_code} — {resp.text[:200]}"
    )
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_update_prompt_returns_403(user_client: TestClient) -> None:
    """普通用户 PUT /prompts/{id} → 403（auth 先于资源查找）。"""
    resp = user_client.put(f"/api/v1/prompts/{uuid.uuid4()}", json={"name": "x"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_delete_prompt_returns_403(user_client: TestClient) -> None:
    """普通用户 DELETE /prompts/{id} → 403。"""
    resp = user_client.delete(f"/api/v1/prompts/{uuid.uuid4()}")
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_update_model_returns_403(user_client: TestClient) -> None:
    """普通用户 PUT /models/{alias} → 403。"""
    resp = user_client.put("/api/v1/models/some-alias", json={"is_active": False})
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_delete_model_returns_403(user_client: TestClient) -> None:
    """普通用户 DELETE /models/{alias} → 403。"""
    resp = user_client.delete("/api/v1/models/some-alias")
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_create_prompt_version_returns_403(user_client: TestClient) -> None:
    """普通用户 POST /prompts/{id}/versions → 403。"""
    resp = user_client.post(
        f"/api/v1/prompts/{uuid.uuid4()}/versions", json={"content": "x"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_non_admin_rollback_prompt_version_returns_403(user_client: TestClient) -> None:
    """普通用户 POST /prompts/{id}/versions/{vid}/rollback → 403。"""
    resp = user_client.post(
        f"/api/v1/prompts/{uuid.uuid4()}/versions/{uuid.uuid4()}/rollback"
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


# ===================== 200：普通用户访问 user 级端点（正向校验）=====================

_USER_LEVEL_GET = [
    "/api/v1/prompts",
    "/api/v1/agents",
    "/api/v1/workflows",
    "/api/v1/models",
    "/api/v1/knowledge-bases",
    "/api/v1/analytics/dashboard",
    "/api/v1/analytics/conversations",
    "/api/v1/evals",
]


@pytest.mark.parametrize("path", _USER_LEVEL_GET)
def test_regular_user_can_access_user_level_endpoints(
    user_client: TestClient, path: str
) -> None:
    """普通用户访问 user 级 GET 端点 → 200（非 403，正向校验）。"""
    resp = user_client.get(path)
    assert resp.status_code == 200, (
        f"GET {path}: expected 200, got {resp.status_code} — {resp.text[:200]}"
    )
