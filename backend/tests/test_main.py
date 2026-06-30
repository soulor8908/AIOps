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

from typing import Any

from fastapi.testclient import TestClient

from app.core.exceptions import NotFoundError


# ===================== health =====================

def test_health_endpoint(client: TestClient) -> None:
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
