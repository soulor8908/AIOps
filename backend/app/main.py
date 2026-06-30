"""AIOps Console — FastAPI 入口（< 120 行）。

职责（遵循 `specs/errors.spec.md`§5、`specs/observability.spec.md`§4、
`specs/security.spec.md`§4）：
- lifespan：启动时建表（开发期），关闭连接池
- CORS 中间件（methods/headers 显式列举，禁止通配）
- request_id 中间件：每请求分配 UUID，写入 ``request.state`` 与 ``X-Request-ID`` 响应头
- 全局异常处理器：``AppError`` / ``RequestValidationError`` / ``Exception`` 兜底
- 挂载聚合路由
- ``/health`` 健康检查
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import api_router
from app.core.config import settings
from app.core.database import engine, init_db
from app.core.exceptions import AppError

logger = logging.getLogger("app.main")
logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动建表（``create_all`` 幂等），关闭 engine。

    不再以 ``settings.debug`` 为门槛——``create_all`` 对已存在的表是 no-op，
    生产环境通过 Alembic 迁移建表时 ``create_all`` 不会破坏既有 schema。
    同时避免 ``debug=True`` 导致 Starlette ``ServerErrorMiddleware`` 返回
    明文 traceback（违反 `errors.spec.md`§5.4 禁止泄漏 ``str(exc)``）。
    """
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(
    title="AIOps Console",
    description="AI 原生运营控制台后端",
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    # security.spec.md§4：methods/headers 显式列举，不使用通配。
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ],
    expose_headers=[
        "X-Request-ID",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    ],
)


class RequestIDMiddleware:
    """纯 ASGI 中间件：为每个入站请求分配 request_id 并回传响应头。

    使用纯 ASGI 而非 ``BaseHTTPMiddleware``，避免后者在异常传播与流式响应上的
    已知缺陷（`observability.spec.md`§4：尊重上游 ``X-Request-ID``，否则生成 UUID v4）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = value.decode("latin-1")
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        # 写入 scope["state"] 供下游 handler 读取（等价于 request.state.request_id）
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                # 移除上游可能已存在的同名头，避免重复
                headers = [(k, v) for k, v in headers if k != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)


app.add_middleware(RequestIDMiddleware)


# ===================== 异常处理器（errors.spec.md§5） =====================

def _json_response(request: Request, status_code: int, content: dict[str, Any]) -> JSONResponse:
    """构造统一 JSON 响应，并附 ``X-Request-ID`` 头。

    ``ServerErrorMiddleware`` 位于 ``RequestIDMiddleware`` 外层，异常处理器
    生成的响应不经过 ``RequestIDMiddleware``，因此需在此显式补充。
    """
    response = JSONResponse(status_code=status_code, content=content)
    response.headers["X-Request-ID"] = getattr(request.state, "request_id", "-")
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """应用异常 → 统一 JSON。``detail`` 为 None 时省略（errors.spec.md§2）。"""
    return _json_response(request, exc.status_code, exc.to_response())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic 校验失败 → 422 统一格式（errors.spec.md§5.3）。

    ``detail`` 保留 FastAPI 原始字段级错误数组（含 loc/msg/type）。
    """
    return _json_response(
        request,
        422,
        {
            "error": "validation_error",
            "message": "输入校验失败",
            "detail": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """500 兜底（errors.spec.md§5.4）：禁止将 str(exc) 写入响应体。"""
    request_id = getattr(request.state, "request_id", "-")
    logger.exception("unhandled error | request_id=%s", request_id)
    return _json_response(
        request,
        500,
        {"error": "internal_error", "message": "服务器内部错误"},
    )


# ===================== 健康检查（deployment.spec.md§6） =====================

@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """健康检查。返回 status + version。"""
    return {"status": "ok", "version": settings.app_version}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """根路径。"""
    return {"service": "aiops-console", "docs": "/docs"}


app.include_router(api_router)
