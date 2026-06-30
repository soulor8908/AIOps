"""Eval Suite API 契约测试 (/api/v1/evals)。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401


def _create_eval(
    client: TestClient,
    *,
    name: str = "basic-eval",
) -> dict:
    payload = {
        "name": name,
        "description": "a basic eval",
        "cases": [
            {"input": "q1", "expected": "answer1", "name": "c1"},
            {"input": "q2", "expected": "answer2"},
        ],
        "rules": [
            {"name": "exact-match", "judge_type": "exact", "expected": "answer1"},
        ],
        "judge_type": "exact",
    }
    resp = client.post("/api/v1/evals", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_evals_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/evals")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_eval_success(client: TestClient) -> None:
    body = _create_eval(client, name="my-eval")
    assert isinstance(body["id"], str)
    uuid.UUID(body["id"])
    assert body["name"] == "my-eval"
    assert body["description"] == "a basic eval"
    assert body["judge_type"] == "exact"
    assert body["status"] == "pending"
    assert body["pass_count"] == 0
    assert body["fail_count"] == 0
    assert body["score"] is None
    assert body["results"] is None
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)
    # cases / rules 以 JSON 结构存回
    assert isinstance(body["cases"], list) and len(body["cases"]) == 2
    assert body["cases"][0]["input"] == "q1"
    assert body["cases"][0]["expected"] == "answer1"
    assert isinstance(body["rules"], list) and len(body["rules"]) == 1
    assert body["rules"][0]["name"] == "exact-match"


def test_create_eval_validation_error(client: TestClient) -> None:
    # service 层强制：cases 为空时抛 ValidationError (AppError 422)
    resp = client.post("/api/v1/evals", json={"name": "no-cases"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert "message" in body

    # 缺少必填字段 name（Pydantic 422）
    resp = client.post("/api/v1/evals", json={"cases": [{"input": "x"}]})
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_get_eval_by_id(client: TestClient) -> None:
    created = _create_eval(client, name="fetch-eval")
    resp = client.get(f"/api/v1/evals/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["name"] == "fetch-eval"
    assert len(body["cases"]) == 2


def test_get_eval_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/evals/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert "message" in body
