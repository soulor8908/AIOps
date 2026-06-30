"""Prompt Studio API 契约测试 (/api/v1/prompts)。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401


def _create_prompt(
    client: TestClient,
    *,
    name: str = "greeting",
    content: str = "Hello {name}",
    variables: list[str] | None = None,
    description: str | None = "a greeting prompt",
) -> dict:
    payload = {
        "name": name,
        "content": content,
        "variables": variables if variables is not None else ["name"],
        "description": description,
    }
    resp = client.post("/api/v1/prompts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_prompts_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_prompt_success(client: TestClient) -> None:
    body = _create_prompt(client, name="welcome", content="Welcome {user}")
    # 基础字段
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])  # 合法 UUID
    assert body["name"] == "welcome"
    assert body["description"] == "a greeting prompt"
    assert body["is_active"] is True
    assert body["current_version_id"] is not None
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)
    # 初始版本
    versions = body["versions"]
    assert isinstance(versions, list)
    assert len(versions) == 1
    v1 = versions[0]
    assert v1["version_num"] == 1
    assert v1["content"] == "Welcome {user}"
    assert v1["variables"] == ["name"]
    assert v1["change_note"] == "initial"
    assert v1["prompt_id"] == body["id"]
    assert v1["id"] == body["current_version_id"]


def test_create_prompt_validation_error(client: TestClient) -> None:
    # 缺少必填字段 name / content
    resp = client.post("/api/v1/prompts", json={"description": "no name/content"})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert isinstance(body["detail"], list) and len(body["detail"]) >= 1


def test_get_prompt_by_id(client: TestClient) -> None:
    created = _create_prompt(client, name="fetch-me", content="hi")
    resp = client.get(f"/api/v1/prompts/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["name"] == "fetch-me"
    assert len(body["versions"]) == 1


def test_get_prompt_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/prompts/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_update_prompt(client: TestClient) -> None:
    created = _create_prompt(client, name="orig-name", content="c")
    resp = client.put(
        f"/api/v1/prompts/{created['id']}",
        json={"name": "new-name", "description": "updated desc", "is_active": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "new-name"
    assert body["description"] == "updated desc"
    assert body["is_active"] is False
    assert body["id"] == created["id"]


def test_delete_prompt(client: TestClient) -> None:
    created = _create_prompt(client, name="to-delete", content="c")
    resp = client.delete(f"/api/v1/prompts/{created['id']}")
    assert resp.status_code == 204
    assert resp.content == b""
    # 删除后再 GET 应 404
    assert client.get(f"/api/v1/prompts/{created['id']}").status_code == 404


def test_list_prompt_versions(client: TestClient) -> None:
    created = _create_prompt(client, name="versions", content="v1 content")
    # 新增第二个版本
    resp = client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"content": "v2 content", "change_note": "tweak"},
    )
    assert resp.status_code == 201
    resp = client.get(f"/api/v1/prompts/{created['id']}/versions")
    assert resp.status_code == 200
    versions = resp.json()
    assert isinstance(versions, list)
    assert len(versions) == 2
    nums = sorted(v["version_num"] for v in versions)
    assert nums == [1, 2]
    for v in versions:
        assert isinstance(v["id"], str)
        assert v["prompt_id"] == created["id"]
        assert isinstance(v["content"], str)


def test_create_prompt_version(client: TestClient) -> None:
    created = _create_prompt(client, name="ver-create", content="v1 content")
    resp = client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"content": "v2 content", "variables": ["x"], "change_note": "second"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["version_num"] == 2
    assert body["content"] == "v2 content"
    assert body["variables"] == ["x"]
    assert body["change_note"] == "second"
    assert body["prompt_id"] == created["id"]
    # current_version_id 应指向新版本
    got = client.get(f"/api/v1/prompts/{created['id']}").json()
    assert got["current_version_id"] == body["id"]


def test_rollback_prompt_version(client: TestClient) -> None:
    created = _create_prompt(client, name="rollback", content="original")
    # 取 v1 id
    v1 = client.get(f"/api/v1/prompts/{created['id']}/versions").json()[0]
    # 创建 v2（不同内容）
    client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"content": "modified"},
    )
    # 回滚到 v1
    resp = client.post(
        f"/api/v1/prompts/{created['id']}/versions/{v1['id']}/rollback"
    )
    assert resp.status_code == 201
    body = resp.json()
    # 回滚即追加新版本
    assert body["version_num"] == 3
    assert body["content"] == "original"
    assert "rollback" in (body["change_note"] or "")


def test_diff_prompt_versions(client: TestClient) -> None:
    created = _create_prompt(client, name="diff", content="line1\nline2")
    client.post(
        f"/api/v1/prompts/{created['id']}/versions",
        json={"content": "line1\nline2\nline3"},
    )
    resp = client.get(
        f"/api/v1/prompts/{created['id']}/diff",
        params={"from": 1, "to": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_version"] == 1
    assert body["to_version"] == 2
    assert isinstance(body["added_lines"], list)
    assert isinstance(body["removed_lines"], list)
    assert isinstance(body["unified_diff"], list)
    assert any("line3" in line for line in body["added_lines"])


def test_list_prompts_with_pagination(client: TestClient) -> None:
    for i in range(3):
        _create_prompt(client, name=f"page-{i}", content=f"c{i}")
    # 第一页
    resp = client.get("/api/v1/prompts", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    # 第二页
    resp = client.get("/api/v1/prompts", params={"limit": 2, "offset": 2})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_prompts_with_search(client: TestClient) -> None:
    _create_prompt(client, name="alpha", content="c")
    _create_prompt(client, name="beta", content="c")
    _create_prompt(client, name="alphabet", content="c")
    resp = client.get("/api/v1/prompts", params={"q": "alph"})
    assert resp.status_code == 200
    names = sorted(p["name"] for p in resp.json())
    assert names == ["alpha", "alphabet"]
