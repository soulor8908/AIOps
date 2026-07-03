"""app/main.py 单元测试 — FastAPI 入口端点。

使用 TestClient（由 tests/conftest.py 的 ``client`` fixture 提供 SQLite 隔离）。

覆盖：
- /health 健康检查
- / 根路径
- AppError 全局异常处理器
- 422 验证错误统一格式
- CORS 头
- /docs 和 /openapi.json 可访问
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError

# ===================== health =====================

def test_health_endpoint(client: TestClient, healthy_deps: None) -> None:
    """GET /health 返回 200 和 {status: ok, version}。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert isinstance(data["version"], str)


# ===================== root =====================

def test_root_endpoint(client: TestClient) -> None:
    """GET / 返回 {service: aiops-console}。"""
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "aiops-console"
    assert "docs" in data


# ===================== AppError handler =====================

def test_app_error_handler(client: TestClient) -> None:
    """触发 AppError 时返回统一错误格式。"""
    from app.main import app

    async def raise_not_found() -> None:
        raise NotFoundError("resource missing", detail="id=abc")

    app.add_api_route("/__test_app_error__", raise_not_found, methods=["GET"])
    try:
        resp = client.get("/__test_app_error__")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"] == "not_found"
        assert body["message"] == "resource missing"
        assert body["detail"] == "id=abc"
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_app_error__"
        ]


def test_app_error_handler_different_status(client: TestClient) -> None:
    """不同 AppError 子类返回不同 status_code 与 error_code。"""
    from app.core.exceptions import AuthenticationError
    from app.main import app

    async def raise_auth_error() -> None:
        raise AuthenticationError("token invalid")

    app.add_api_route("/__test_auth_err__", raise_auth_error, methods=["GET"])
    try:
        resp = client.get("/__test_auth_err__")
        assert resp.status_code == 401
        body = resp.json()
        # 对齐 errors.spec.md§4：AuthenticationError.error_code = token_invalid
        assert body["error"] == "token_invalid"
        assert body["message"] == "token invalid"
        assert "detail" not in body
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_auth_err__"
        ]


# ===================== validation error =====================

def test_validation_error_handler(client: TestClient) -> None:
    """发送无效数据时返回 422 统一格式（errors.spec.md§5.3）。"""
    # AgentCreate 要求 name min_length=1，发送空 name 应触发 422
    resp = client.post("/api/v1/agents", json={"name": ""})
    assert resp.status_code == 422
    body = resp.json()
    # 统一格式：{error, message, detail}，detail 为字段级错误数组
    assert body["error"] == "validation_error"
    assert body["message"] == "输入校验失败"
    assert isinstance(body["detail"], list)
    assert len(body["detail"]) > 0
    # 每条错误至少含 loc/msg/type
    first = body["detail"][0]
    assert "loc" in first
    assert "msg" in first
    assert "type" in first


def test_validation_error_missing_body(client: TestClient) -> None:
    """缺少必填 body 时返回 422 统一格式。"""
    resp = client.post("/api/v1/agents")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_error"
    assert body["message"] == "输入校验失败"
    assert isinstance(body["detail"], list)


# ===================== CORS =====================

def test_cors_headers(client: TestClient) -> None:
    """验证 CORS 头存在（security.spec.md§4：仅允许白名单 Origin）。"""
    # 使用配置中允许的开发 Origin
    resp = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    # CORS 中间件应回放允许的 Origin（非通配）
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_rejected_origin(client: TestClient) -> None:
    """非白名单 Origin 不应回放 Access-Control-Allow-Origin。"""
    resp = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 200
    # 非白名单 Origin 不应被允许
    assert resp.headers.get("access-control-allow-origin") is None


def test_cors_preflight(client: TestClient) -> None:
    """CORS 预检请求返回正确头（security.spec.md§4：methods 显式列举）。"""
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    # 显式列举的方法（禁止通配 *）
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    assert "GET" in allow_methods
    assert "*" not in allow_methods


# ===================== OpenAPI docs =====================

def test_openapi_docs_available(client: TestClient) -> None:
    """/docs 和 /openapi.json 可访问。"""
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")

    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["info"]["title"] == "AIOps Console"
    # 确认路径已注册
    assert "/health" in data["paths"]
    assert "/" in data["paths"]


def test_openapi_contains_api_routes(client: TestClient) -> None:
    """OpenAPI 包含 /api/v1 路由。"""
    resp = client.get("/openapi.json")
    data = resp.json()
    api_paths = [p for p in data["paths"] if p.startswith("/api/v1")]
    assert len(api_paths) > 0


# ===================== request_id 中间件（observability.spec.md§4） =====================

def test_request_id_generated_when_absent(client: TestClient) -> None:
    """未携带 X-Request-ID 时服务端生成 UUID 并回传。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    # 应为合法 UUID v4
    import uuid as _uuid

    parsed = _uuid.UUID(rid)
    assert parsed.version == 4


def test_request_id_passthrough_when_present(client: TestClient) -> None:
    """携带 X-Request-ID 时服务端原样回传（observability.spec.md§4）。"""
    custom = "my-trace-id-12345"
    resp = client.get("/health", headers={"X-Request-ID": custom})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == custom


# ===================== 500 兜底处理器（errors.spec.md§5.4） =====================

def test_unhandled_exception_returns_500(client: TestClient) -> None:
    """未捕获异常 → 500 统一格式，禁止泄漏 str(exc)。

    Starlette 的 ``ServerErrorMiddleware`` 在发送 500 响应后会重新抛出异常
    用于日志记录；TestClient 默认 ``raise_server_exceptions=True`` 会捕获
    该重抛异常。此处关闭以验证实际响应体。
    """
    from app.main import app

    async def raise_unhandled() -> None:
        # 模拟未捕获的 ValueError（非 AppError）
        raise ValueError("database connection string contains password")

    app.add_api_route("/__test_unhandled__", raise_unhandled, methods=["GET"])
    # 关闭 server exception 重新抛出，以验证 500 响应体
    # Starlette 1.x 将该标志存储在 transport 上
    transport = client._transport
    original = transport.raise_server_exceptions
    transport.raise_server_exceptions = False
    try:
        resp = client.get("/__test_unhandled__")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        assert body["message"] == "服务器内部错误"
        # 关键：禁止泄漏异常字符串
        assert "detail" not in body
        assert "database connection" not in resp.text
        # 仍应回传 request_id
        assert resp.headers.get("X-Request-ID") is not None
    finally:
        transport.raise_server_exceptions = original
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_unhandled__"
        ]


# ===================== to_response：detail 省略（errors.spec.md§2） =====================

def test_app_error_detail_omitted_when_none(client: TestClient) -> None:
    """detail 为 None 时响应体省略 detail 字段。"""
    from app.core.exceptions import NotFoundError
    from app.main import app

    async def raise_no_detail() -> None:
        raise NotFoundError("missing")

    app.add_api_route("/__test_no_detail__", raise_no_detail, methods=["GET"])
    try:
        resp = client.get("/__test_no_detail__")
        assert resp.status_code == 404
        body = resp.json()
        assert body == {"error": "not_found", "message": "missing"}
        assert "detail" not in body
    finally:
        app.router.routes = [
            r for r in app.router.routes
            if getattr(r, "path", "") != "/__test_no_detail__"
        ]


# ===================== /metrics 端点（observability.spec.md§5） =====================

def test_metrics_endpoint_empty(client: TestClient) -> None:
    """无请求时 /metrics 返回 Prometheus 格式（空或仅含元数据行）。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        # Prometheus exposition format 的 content-type
        assert "text/plain" in resp.headers.get("content-type", "")
        # 空时返回空字符串或仅换行
        body = resp.text
        assert isinstance(body, str)
    finally:
        metrics.reset()


def test_metrics_endpoint_after_request(client: TestClient) -> None:
    """发送请求后 /metrics 含 request_count + request_latency。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        # 触发一次请求
        client.get("/health")
        resp = client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        # 应含 request_count counter
        assert "# TYPE request_count counter" in body
        # 应含 request_latency histogram
        assert "# TYPE request_latency histogram" in body
        assert "request_latency_bucket" in body
        assert "request_latency_count" in body
        assert "request_latency_sum" in body
        # 应含 /health endpoint 的指标
        assert 'endpoint="/health"' in body
    finally:
        metrics.reset()


# ===================== 可观测性中间件（observability.spec.md§2/§4/§5） =====================

def test_request_context_set_during_request(client: TestClient) -> None:
    """请求处理期间 ContextVar 持有 request_id，结束后清理。"""
    from app.core.logging import request_id_var

    # 请求前应为 None
    assert request_id_var.get() is None
    client.get("/health")
    # 请求结束后应清理（finally 中 clear_request_context）
    assert request_id_var.get() is None


def test_latency_recorded_in_metrics(client: TestClient) -> None:
    """请求耗时被记录到 request_latency histogram。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        client.get("/health")
        state = metrics.get_histogram("request_latency", ("/health",))
        assert state.count >= 1
        assert state.sum > 0  # latency_ms 应为正数
    finally:
        metrics.reset()


# ===================== endpoint 标签归一化（防高基数） =====================

def test_endpoint_label_uses_route_template(client: TestClient) -> None:
    """参数化路由的 endpoint 标签使用路由模板而非原始路径（防 Prometheus 高基数）。

    P1-1 修复：访问 /api/v1/agents/{uuid} 时，metrics 中的 endpoint 标签应为
    路由模板（含 {agent_id} 占位符）或归一化后的 /{id}，而非原始 UUID 路径。
    """
    from app.core.metrics import metrics

    metrics.reset()
    try:
        # 访问不存在的 agent UUID（会返回 404，但中间件仍记录指标）
        fake_uuid = "12345678-1234-1234-1234-123456789012"
        client.get(f"/api/v1/agents/{fake_uuid}")
        out = metrics.render_prometheus()
        # 关键：原始 UUID 不应作为 endpoint 标签值（防高基数）
        assert fake_uuid not in out
        # 应出现归一化后的 endpoint（路由模板含 {agent_id} 或 {id} 占位符）
        assert "{agent_id}" in out or "{id}" in out, (
            f"endpoint 未归一化为路由模板: {out}"
        )
    finally:
        metrics.reset()


def test_metrics_endpoint_label_uses_template(client: TestClient) -> None:
    """/metrics 自身的 endpoint 标签使用路由模板 /metrics。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        client.get("/metrics")
        out = metrics.render_prometheus()
        assert 'endpoint="/metrics"' in out
    finally:
        metrics.reset()


# ===================== request_id 贯穿日志链路（observability.spec.md§4/§8） =====================

def test_request_id_propagates_to_request_log(
    client: TestClient, caplog: pytest.LogCaptureFixture[str], healthy_deps: None
) -> None:
    """request_id 贯穿到请求结束日志行（observability.spec.md§4/§8 端到端验证）。

    携带 X-Request-ID 请求 → 该值同时出现在响应头与 "request completed" 日志记录，
    证明 ContextVar 注入链路完整（中间件 set → filter 注入 → 日志携带）。
    """
    import logging

    from app.core.logging import RequestContextFilter

    # caplog 自带 handler 无 RequestContextFilter，手动挂载使 request_id 注入捕获记录
    caplog.set_level(logging.INFO, logger="app.main")
    caplog.handler.addFilter(RequestContextFilter())

    custom_rid = "trace-e2e-abc-123"
    resp = client.get("/health", headers={"X-Request-ID": custom_rid})
    assert resp.status_code == 200
    # 响应头回传（§4）
    assert resp.headers["X-Request-ID"] == custom_rid

    # 请求结束日志应携带同一 request_id（§4 贯穿日志）
    completed = [
        r for r in caplog.records if r.getMessage() == "request completed"
    ]
    assert completed, "未捕获到 request completed 日志"
    assert getattr(completed[-1], "request_id", None) == custom_rid


# ===================== 错误率指标（observability.spec.md§5.1 error_rate） =====================

def test_4xx_recorded_in_request_count(client: TestClient) -> None:
    """4xx 请求被记入 request_count（error_rate 可观测）。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        # 触发 404（GET 不存在的 prompt）
        import uuid

        resp = client.get(f"/api/v1/prompts/{uuid.uuid4()}")
        assert resp.status_code == 404
        # 404 应出现在 request_count（中间件对所有 HTTP 请求记录指标）。
        # endpoint 标签为路由模板（不含 /api/v1 前缀，scope["route"].path 返回子路由路径）。
        assert metrics.get_counter("request_count", ("GET", "/prompts/{prompt_id}", "404")) >= 1.0
    finally:
        metrics.reset()


def test_5xx_recorded_in_request_count(client: TestClient) -> None:
    """5xx 请求被记入 request_count（error_rate 可观测，§5.1）。"""
    from app.core.metrics import metrics
    from app.main import app

    async def raise_500() -> None:
        raise RuntimeError("simulated failure")

    app.add_api_route("/__test_500__", raise_500, methods=["GET"])
    transport = client._transport
    original = transport.raise_server_exceptions
    transport.raise_server_exceptions = False

    metrics.reset()
    try:
        resp = client.get("/__test_500__")
        assert resp.status_code == 500
        # 500 应出现在 request_count（异常路径 effective_status=500）
        assert metrics.get_counter("request_count", ("GET", "/__test_500__", "500")) >= 1.0
    finally:
        transport.raise_server_exceptions = original
        metrics.reset()

