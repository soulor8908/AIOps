"""Model Router API 契约测试 (/api/v1/models)。

注意：Decimal 字段（cost_per_1k_*）经 FastAPI 序列化为字符串，故用
``float(...)`` 做数值断言以兼容字符串/数值两种形态。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401


def _create_model(
    client: TestClient,
    *,
    alias: str = "gpt-4o",
    model_name: str = "gpt-4o",
    is_active: bool = True,
    priority: int = 100,
) -> dict:
    payload = {
        "alias": alias,
        "provider": "openai",
        "model_name": model_name,
        "max_tokens": 8192,
        "temperature": 0.5,
        "cost_per_1k_input": "0.01",
        "cost_per_1k_output": "0.03",
        "priority": priority,
        "is_active": is_active,
    }
    resp = client.post("/api/v1/models", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_models_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/models")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_model_success(client: TestClient) -> None:
    body = _create_model(client, alias="my-alias", model_name="gpt-4o-mini")
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["alias"] == "my-alias"
    assert body["provider"] == "openai"
    assert body["model_name"] == "gpt-4o-mini"
    assert body["max_tokens"] == 8192
    assert body["temperature"] == 0.5
    assert body["is_active"] is True
    assert body["priority"] == 100
    # Decimal 字段序列化为字符串
    assert float(body["cost_per_1k_input"]) == 0.01
    assert float(body["cost_per_1k_output"]) == 0.03
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_create_model_validation_error(client: TestClient) -> None:
    # 缺少必填字段 alias / model_name
    resp = client.post("/api/v1/models", json={"provider": "openai"})
    assert resp.status_code == 422
    assert "detail" in resp.json()

    # temperature 超范围 (>2.0)
    resp = client.post(
        "/api/v1/models",
        json={"alias": "bad", "model_name": "m", "temperature": 5.0},
    )
    assert resp.status_code == 422


def test_get_model_by_alias(client: TestClient) -> None:
    _create_model(client, alias="by-alias", model_name="gpt-4o")
    resp = client.get("/api/v1/models/by-alias")
    assert resp.status_code == 200
    body = resp.json()
    assert body["alias"] == "by-alias"
    assert body["model_name"] == "gpt-4o"


def test_get_model_not_found(client: TestClient) -> None:
    resp = client.get("/api/v1/models/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_update_model(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # SQLite/aiosqlite 不支持 onupdate=func.now() 的 RETURNING，
    # 导致 flush 后 updated_at 属性过期，model_validate 同步访问触发 MissingGreenlet。
    # PostgreSQL 生产环境通过 RETURNING 自动填充，此处仅 SQLite 测试需要 refresh 补偿。
    import app.domains.models.service as model_service

    _original_update = model_service.update_model

    async def _refreshing_update_model(session, alias, payload):  # type: ignore[no-untyped-def]
        config = await _original_update(session, alias, payload)
        await session.refresh(config)
        return config

    monkeypatch.setattr(model_service, "update_model", _refreshing_update_model)

    _create_model(client, alias="upd", model_name="m1")
    resp = client.put(
        "/api/v1/models/upd",
        json={"model_name": "m2", "is_active": False, "temperature": 0.1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["alias"] == "upd"
    assert body["model_name"] == "m2"
    assert body["is_active"] is False
    assert body["temperature"] == 0.1


def test_delete_model(client: TestClient) -> None:
    _create_model(client, alias="del", model_name="m")
    resp = client.delete("/api/v1/models/del")
    assert resp.status_code == 204
    assert resp.content == b""
    # 删除后再 GET 应 404
    assert client.get("/api/v1/models/del").status_code == 404


def test_list_models_active_only(client: TestClient) -> None:
    _create_model(client, alias="active-one", model_name="m1", is_active=True)
    _create_model(client, alias="inactive-one", model_name="m2", is_active=False)

    # active_only=true 仅返回启用模型
    resp = client.get("/api/v1/models", params={"active_only": "true"})
    assert resp.status_code == 200
    active = resp.json()
    assert len(active) == 1
    assert active[0]["alias"] == "active-one"
    assert all(m["is_active"] for m in active)

    # 默认（active_only=false）返回全部
    resp = client.get("/api/v1/models", params={"active_only": "false"})
    assert resp.status_code == 200
    all_models = resp.json()
    assert len(all_models) == 2
    assert {m["alias"] for m in all_models} == {"active-one", "inactive-one"}
