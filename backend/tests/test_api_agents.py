"""Agent Orchestrator API 契约测试 (/api/v1/agents, /api/v1/workflows)。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401


def _create_agent(
    client: TestClient,
    *,
    name: str = "researcher",
    description: str | None = "a research agent",
) -> dict:
    payload = {
        "name": name,
        "description": description,
        "system_prompt": "you are a researcher",
        "model_alias": "default",
        "tools": [{"name": "search", "type": "search", "description": "web search"}],
        "max_turns": 5,
        "temperature": 0.3,
    }
    resp = client.post("/api/v1/agents", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_agents_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_agent_success(client: TestClient) -> None:
    body = _create_agent(client, name="bot")
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["name"] == "bot"
    assert body["description"] == "a research agent"
    assert body["system_prompt"] == "you are a researcher"
    assert body["model_alias"] == "default"
    assert body["max_turns"] == 5
    assert body["temperature"] == 0.3
    assert body["is_active"] is True
    assert isinstance(body["tools"], list)
    assert body["tools"][0]["name"] == "search"
    assert body["tools"][0]["type"] == "search"
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_create_agent_validation_error(client: TestClient) -> None:
    # 缺少必填字段 name
    resp = client.post("/api/v1/agents", json={"description": "no name"})
    assert resp.status_code == 422
    assert "detail" in resp.json()

    # max_turns 超上限（<=10）也应 422
    resp = client.post(
        "/api/v1/agents", json={"name": "x", "max_turns": 99}
    )
    assert resp.status_code == 422


def test_get_agent_by_id(client: TestClient) -> None:
    created = _create_agent(client, name="fetch-me")
    resp = client.get(f"/api/v1/agents/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["name"] == "fetch-me"


def test_get_agent_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/agents/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"


def test_list_workflows_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/workflows")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_workflow_success(client: TestClient) -> None:
    payload = {
        "name": "rag-flow",
        "description": "retrieve then answer",
        "nodes": [
            {"id": "n1", "name": "entry", "is_entry": True, "inputs": {}},
            {"id": "n2", "name": "exit", "is_exit": True, "inputs": {}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }
    resp = client.post("/api/v1/workflows", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["name"] == "rag-flow"
    assert body["description"] == "retrieve then answer"
    assert body["is_active"] is True
    assert isinstance(body["nodes"], list) and len(body["nodes"]) == 2
    assert isinstance(body["edges"], list) and len(body["edges"]) == 1
    assert body["nodes"][0]["id"] == "n1"
    assert body["edges"][0]["source"] == "n1"
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)


def test_create_workflow_validation_error(client: TestClient) -> None:
    # 缺少必填字段 name
    resp = client.post("/api/v1/workflows", json={"description": "no name"})
    assert resp.status_code == 422
    assert "detail" in resp.json()
