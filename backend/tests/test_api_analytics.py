"""Analytics API 契约测试 (/api/v1/analytics)。

Dashboard 在空库上即可返回完整结构（聚合查询对空表安全），无需预置数据。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app  # noqa: F401


def test_list_conversations_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/analytics/conversations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_conversation_not_found(client: TestClient) -> None:
    resp = client.get(f"/api/v1/analytics/conversations/{uuid.uuid4()}")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_get_dashboard_metrics(client: TestClient) -> None:
    resp = client.get("/api/v1/analytics/dashboard", params={"days": 7})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 验证返回结构包含所有声明字段
    assert "total_conversations" in body
    assert "total_messages" in body
    assert "total_tokens" in body
    assert "total_cost" in body
    assert "avg_messages_per_conversation" in body
    assert "avg_latency_ms" in body
    assert "active_models" in body
    assert "conversations_last_7d" in body

    # 字段类型校验
    assert isinstance(body["total_conversations"], int)
    assert isinstance(body["total_messages"], int)
    assert isinstance(body["total_tokens"], int)
    assert float(body["total_cost"]) == 0.0  # 空库，Decimal 序列化为字符串
    assert isinstance(body["avg_messages_per_conversation"], (int, float))
    assert isinstance(body["avg_latency_ms"], (int, float))
    assert isinstance(body["active_models"], list)
    assert isinstance(body["conversations_last_7d"], list)

    # 空库下数值应为 0
    assert body["total_conversations"] == 0
    assert body["total_messages"] == 0
    assert body["total_tokens"] == 0
    assert body["active_models"] == []
    assert body["conversations_last_7d"] == []


def test_get_dashboard_metrics_days_param_validation(client: TestClient) -> None:
    # days 超范围 (>90) 应 422
    resp = client.get("/api/v1/analytics/dashboard", params={"days": 999})
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_get_ai_health_metrics_admin(client: TestClient) -> None:
    """admin 可访问 /analytics/ai-health，返回完整健康度结构（P2-9）。"""
    resp = client.get("/api/v1/analytics/ai-health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 验证返回结构包含所有声明字段
    assert "llm_error_rate" in body
    assert "tool_call_success_rate" in body
    assert "avg_latency_ms" in body
    assert "active_model_count" in body
    assert "total_llm_calls" in body
    assert "total_llm_errors" in body
    # 类型校验
    assert isinstance(body["llm_error_rate"], (int, float))
    assert isinstance(body["tool_call_success_rate"], (int, float))
    assert isinstance(body["avg_latency_ms"], (int, float))
    assert isinstance(body["active_model_count"], int)
    assert isinstance(body["total_llm_calls"], int)
    assert isinstance(body["total_llm_errors"], int)
    # 空库 + 无 LLM 调用记录：错误率为 0，tool 成功率默认 1.0
    assert body["llm_error_rate"] == 0.0
    assert body["tool_call_success_rate"] == 1.0
    assert body["active_model_count"] == 0


def test_get_ai_health_metrics_requires_admin(user_client: TestClient) -> None:
    """普通用户访问 /analytics/ai-health 应 403（与 /metrics 鉴权一致，P2-9）。"""
    resp = user_client.get("/api/v1/analytics/ai-health")
    assert resp.status_code == 403
