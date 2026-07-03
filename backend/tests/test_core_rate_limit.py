"""Rate limit middleware 测试（security.spec.md§5）。

用 fakeredis 验证 Redis 滑动窗口限流：
- 默认 100 req/min per user / per IP
- LLM 端点 20 req/min（独立配额，与默认互不影响）
- 超限返回 429 + X-RateLimit-* 头
- per-user keying（不同用户独立配额）
- Redis 不可用时降级放行
"""

from __future__ import annotations

import fakeredis.aioredis
from fastapi.testclient import TestClient

from app.core.jwt import create_access_token


def _patch_fakeredis(monkeypatch) -> fakeredis.aioredis.FakeRedis:  # type: ignore[no-untyped-def]
    """用 fakeredis 覆盖 conftest 的 _skip_rate_limit，返回新实例。"""
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr("app.core.rate_limit.get_redis", lambda: fake)
    return fake


def test_rate_limit_headers_on_success(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """未超限请求返回 200 + X-RateLimit-* 头。"""
    _patch_fakeredis(monkeypatch)
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-limit"] == "100"
    assert resp.headers["x-ratelimit-remaining"] == "99"
    assert "x-ratelimit-reset" in resp.headers


def test_rate_limit_429_when_exceeded(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """超过 100/min 配额返回 429 + 限流头。"""
    _patch_fakeredis(monkeypatch)
    for _ in range(100):
        resp = client.get("/api/v1/prompts")
        assert resp.status_code == 200
    # 第 101 个请求超限
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert resp.headers["x-ratelimit-remaining"] == "0"
    assert resp.headers["x-ratelimit-limit"] == "100"


def test_rate_limit_llm_endpoint_20_per_min(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """LLM 端点使用 20/min 配额（/chat 后缀触发 LLM bucket）。"""
    _patch_fakeredis(monkeypatch)
    # 20 个请求在配额内（路由层可能 422/404，但不应 429）
    for _ in range(20):
        resp = client.post("/api/v1/models/default/chat", json={"messages": []})
        assert resp.status_code != 429
    # 第 21 个超限
    resp = client.post("/api/v1/models/default/chat", json={"messages": []})
    assert resp.status_code == 429
    assert resp.headers["x-ratelimit-limit"] == "20"


def test_rate_limit_llm_and_default_independent(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """LLM 与默认配额独立计数：20 个 LLM 请求不消耗默认配额。"""
    _patch_fakeredis(monkeypatch)
    # 耗尽 LLM 配额（20 次）
    for _ in range(20):
        client.post("/api/v1/models/default/chat", json={"messages": []})
    # LLM 已超限
    resp = client.post("/api/v1/models/default/chat", json={"messages": []})
    assert resp.status_code == 429
    # 默认端点仍可用（remaining 未受 LLM 计数影响）
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-remaining"] == "99"


def test_rate_limit_per_user_keying(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """已认证请求按 user_id 限流，不同用户独立配额。"""
    _patch_fakeredis(monkeypatch)
    token_a = create_access_token("user-a-id")
    token_b = create_access_token("user-b-id")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    # 用户 A 发 50 个请求
    for _ in range(50):
        resp = client.get("/api/v1/prompts", headers=headers_a)
        assert resp.status_code == 200
    # 用户 B 配额不受影响（remaining = 99）
    resp = client.get("/api/v1/prompts", headers=headers_b)
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-remaining"] == "99"


def test_rate_limit_per_ip_for_unauthenticated(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """未认证请求按 IP 限流。"""
    _patch_fakeredis(monkeypatch)
    # 未携带 token 的请求按 IP 计数
    resp1 = client.get("/api/v1/prompts")
    resp2 = client.get("/api/v1/prompts")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # 两次请求后 remaining 应为 98
    assert resp2.headers["x-ratelimit-remaining"] == "98"


def test_rate_limit_degrades_when_redis_unavailable(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Redis 不可用时降级放行（不返回 429，不注入限流头）。"""
    def _raise() -> None:
        raise ConnectionRefusedError("redis down")

    monkeypatch.setattr("app.core.rate_limit.get_redis", _raise)
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    assert "x-ratelimit-limit" not in resp.headers


def test_rate_limit_skips_non_api_paths(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """非 API 路径（/health）不限流，无限流头。"""
    _patch_fakeredis(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "x-ratelimit-limit" not in resp.headers
