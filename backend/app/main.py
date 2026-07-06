"""AIOps Console — FastAPI 入口（< 200 行）。

职责（遵循 `specs/errors.spec.md`§5、`specs/observability.spec.md`§2/§4/§5、
`specs/security.spec.md`§4）：
- lifespan：启动时建表（开发期），关闭连接池
- 结构化 JSON 日志：``setup_logging`` 在导入期配置 root logger
- CORS 中间件（methods/headers 显式列举，禁止通配）
- 可观测性中间件：request_id 分配 + ContextVar 贯穿 + latency 测量 + 请求结束
  结构化日志 + 指标采集（observability.spec.md§2/§4/§5）
- 全局异常处理器：``AppError`` / ``RequestValidationError`` / ``Exception`` 兜底
- ``/health`` 健康检查 + ``/metrics`` Prometheus 导出
- 挂载聚合路由
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import api_router
from app.core import health as health_mod
from app.core.config import _PROD_ENVS, settings
from app.core.database import engine, init_db
from app.core.deps import get_current_admin
from app.core.exceptions import AppError
from app.core.llm_client import close_all_clients
from app.core.logging import (
    request_id_var,
    reset_request_context,
    set_request_context,
    setup_logging,
)
from app.core.metrics import metrics
from app.core.rate_limit import RateLimitMiddleware
from app.core.redis import close_redis
from app.domains.auth.models import User
from app.domains.knowledge.embedder import close_embedder_client

# 导入期配置 JSON 日志（observability.spec.md§2）。
# 幂等：重复 import 仅重置 handler。测试 conftest import 本模块即生效。
setup_logging(settings.log_level)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动建表（仅开发/测试），关闭 engine。

    生产环境（``environment`` ∈ {production, prod, staging}）必须通过 Alembic
    迁移管理 schema，``create_all`` 在此跳过——避免 ORM 模型与迁移不一致时
    ``create_all`` 跳过未覆盖的新列/索引导致运行时 ``UndefinedColumn`` 错误，
    也避免对已存在的表产生与迁移不符的结构。

    开发/测试环境保留 ``create_all`` 以降低本地启动门槛（无需手动 alembic upgrade）。

    P0-2：``agent_scheduler_enabled=True`` 时启动 autonomous loop worker，
    shutdown 时 cancel 并等待退出。
    """
    if settings.environment.lower() not in _PROD_ENVS:
        await init_db()
    scheduler_task: asyncio.Task[None] | None = None
    if settings.agent_scheduler_enabled:
        from app.domains.agents.scheduler import run_scheduler_loop

        scheduler_task = asyncio.create_task(run_scheduler_loop())
    yield
    # P0-12：shutdown 序列用整体超时包裹，超时记 CRITICAL 后强制退出。
    # K8s terminationGracePeriodSeconds 默认 30s，此处 20s + 10s buffer 对齐。
    # 单步阻塞（scheduler cancel 等待 LLM 调用 / engine.dispose 等长事务）
    # 累计超阈值则放弃等待，避免 pod 被 SIGKILL 时 in-flight 状态丢失无日志。
    async def _shutdown_seq() -> None:
        if scheduler_task is not None:
            scheduler_task.cancel()
            # 二次保护：scheduler tick 内 LLM 调用最多 60s，15s 内必须退出
            try:
                await asyncio.wait_for(
                    asyncio.gather(scheduler_task, return_exceptions=True),
                    timeout=15.0,
                )
            except TimeoutError:
                logger.critical(
                    "scheduler_task shutdown timeout, force exit",
                    extra={"event": "shutdown", "outcome": "timeout", "component": "scheduler"},
                )
        # P0-10：取消所有 fire-and-forget task 并等待退出，防止 event loop 关闭后
        # task 还尝试运行（``Event loop is closed`` 错误）+ 释放强引用。
        from app.core.task_registry import get_task_registry

        await get_task_registry().shutdown()
        await engine.dispose()
        await close_redis()
        await close_embedder_client()
        # P1-5：释放 LLMClient 连接池单例（evals judge 等应用级 client）。
        await close_all_clients()

    try:
        await asyncio.wait_for(
            _shutdown_seq(), timeout=settings.lifespan_shutdown_timeout_seconds
        )
    except TimeoutError:
        logger.critical(
            "lifespan shutdown exceeded timeout, force exit (in-flight state may be lost)",
            extra={
                "event": "shutdown",
                "outcome": "timeout",
                "timeout_seconds": settings.lifespan_shutdown_timeout_seconds,
            },
        )


app = FastAPI(
    title="AIOps Console",
    description="AI 原生运营控制台后端",
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

# 限流中间件（security.spec.md§5）— 注册在 CORS 之前（innermost），
# 使 429 响应能经 CORS 中间件获得 CORS 头。
app.add_middleware(RateLimitMiddleware)

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


class ObservabilityMiddleware:
    """纯 ASGI 可观测性中间件（observability.spec.md§2/§4/§5）。

    单一中间件承担四项职责，避免多中间件叠加的开销与 request_id 透传问题：
    1. request_id 分配：尊重上游 ``X-Request-ID``，否则生成 UUID v4（§4）
    2. ContextVar 设置：``request_id`` 贯穿同一请求所有日志（§2.2 / §4）
    3. latency 测量 + 请求结束结构化日志（§2.2 ``latency_ms``）
    4. 指标采集：``request_count`` / ``request_latency``（§5.1）

    使用纯 ASGI 而非 ``BaseHTTPMiddleware``，避免后者在异常传播与流式响应上的
    已知缺陷。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. 分配 request_id（observability.spec.md§4：尊重上游，否则生成 UUID v4）
        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = value.decode("latin-1")
                break
        if not request_id:
            request_id = str(uuid.uuid4())

        scope.setdefault("state", {})["request_id"] = request_id
        # 2. 设置 ContextVar，使本请求所有日志自动携带 request_id（§2.2 / §4）
        # 返回 token 用于 finally 中 reset（Python contextvars 惯用模式）
        ctx_tokens = set_request_context(request_id)

        # 3. latency 测量起点
        start = time.perf_counter()
        status_code = 0
        method = scope.get("method", "")
        raw_path = scope.get("path", "")

        async def send_with_observation(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
                # 回写 X-Request-ID 响应头（observability.spec.md§4）
                headers = message.get("headers", [])
                # 移除上游可能已存在的同名头，避免重复
                headers = [(k, v) for k, v in headers if k != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                # P0-8：安全响应头（security.spec.md§4）
                # X-Content-Type-Options: 防止 MIME 嗅探
                headers.append((b"x-content-type-options", b"nosniff"))
                # X-Frame-Options: 禁止被 iframe 嵌入（防点击劫持）
                headers.append((b"x-frame-options", b"DENY"))
                # Referrer-Policy: 仅同源时发送 referrer
                headers.append((b"referrer-policy", b"same-origin"))
                # HSTS：仅 HTTPS 下生效，强制浏览器后续走 HTTPS（生产环境 1 年）
                if scope.get("scheme") == "https":
                    headers.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                # CSP：默认禁止内联脚本/样式与外部资源加载，
                # API 服务通常无 HTML 页面，严格 CSP 不影响功能
                headers.append(
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
                )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_observation)
        finally:
            # 4. 请求结束：记录 latency_ms + 指标采集
            latency_ms = (time.perf_counter() - start) * 1000.0
            # 归一化 endpoint 避免高基数标签（Prometheus 反模式）：
            # 优先用路由模板（/api/v1/agents/{agent_id}），回退用正则归一化 UUID/数字段。
            endpoint = _resolve_endpoint(scope, raw_path)
            # 异常导致未发送 http.response.start 时 status_code 仍为 0，归一化为 500
            effective_status = status_code if status_code else 500
            logger.info(
                "request completed",
                extra={
                    "latency_ms": round(latency_ms, 3),
                    "method": method,
                    "endpoint": endpoint,
                    "status": effective_status,
                },
            )
            # 指标采集（observability.spec.md§5.1：request_count / request_latency）
            metrics.record_request(method, endpoint, effective_status, latency_ms)
            # 恢复 ContextVar 到 set 前状态（reset 而非 set(None)，避免协程复用泄漏）
            reset_request_context(ctx_tokens)


# UUID v4 与纯数字路径段归一化为 {id}（回退方案，当 scope["route"] 不可用时）
_PATH_ID_PATTERN = re.compile(
    r"/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+)(?=/|$)"
)


def _resolve_endpoint(scope: Scope, raw_path: str) -> str:
    """解析路由模板作为低基数 endpoint 标签。

    优先从 ``scope["route"]`` 读取 Starlette/FastAPI 匹配的路由模板
    （如 ``/api/v1/agents/{agent_id}``），避免 UUID/数字路径段导致标签基数爆炸。
    若路由未匹配（404）或版本不支持，回退用正则归一化 UUID/数字段为 ``{id}``。
    """
    route = scope.get("route")
    path_template: str | None = getattr(route, "path", None)
    if path_template:
        return path_template
    return _PATH_ID_PATTERN.sub("/{id}", raw_path)


app.add_middleware(ObservabilityMiddleware)


# ===================== 异常处理器（errors.spec.md§5） =====================

def _json_response(request: Request, status_code: int, content: dict[str, Any]) -> JSONResponse:
    """构造统一 JSON 响应。

    ``X-Request-ID`` 头由 ``ObservabilityMiddleware.send_with_observation`` 统一注入，
    此处从 ``request.state`` 兜底设置（``ServerErrorMiddleware`` 路径下中间件不经过时生效）。
    """
    response = JSONResponse(status_code=status_code, content=content)
    # 优先从 ContextVar 读取（与日志一致），回退到 request.state
    rid: str = request_id_var.get() or str(
        getattr(request.state, "request_id", "-")
    )
    response.headers["X-Request-ID"] = rid
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
    """500 兜底（errors.spec.md§5.4）：禁止将 str(exc) 写入响应体。

    request_id 由 ``RequestContextFilter`` 从 ContextVar 注入到 JSON 顶层字段，
    无需在 message 文本中重复（避免日志冗余）。
    """
    logger.exception("unhandled error")
    return _json_response(
        request,
        500,
        {"error": "internal_error", "message": "服务器内部错误"},
    )


# ===================== 健康检查（deployment.spec.md§6） =====================

@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    """健康检查（deployment.spec.md§6）。

    依赖（DB/Redis/LLM）均可达返回 ``ok``，否则 ``degraded``（仍 200，readiness
    probe 据此摘流而非重启）。``checks`` 暴露各项依赖状态便于排障。

    P0-19：新增 LLM 探测——LLM 是核心依赖，provider 故障时 /health 应反映
    degraded 状态，触发 readiness probe 摘流（避免请求路由到 LLM 不可达的 pod）。
    """
    db_ok = await health_mod.check_db()
    redis_ok = await health_mod.check_redis()
    llm_ok = await health_mod.check_llm()
    overall = "ok" if (db_ok and redis_ok and llm_ok) else "degraded"
    return {
        "status": overall,
        "version": settings.app_version,
        "checks": {
            "database": "ok" if db_ok else "down",
            "redis": "ok" if redis_ok else "down",
            "llm": "ok" if llm_ok else "down",
        },
    }


@app.get("/metrics", tags=["meta"])
async def metrics_endpoint(
    _admin: User = Depends(get_current_admin),
) -> PlainTextResponse:
    """Prometheus 指标导出（observability.spec.md§5）。

    需 admin 权限——指标含 request_count/llm_tokens/llm_cost 等运营敏感数据，
    匿名暴露会泄露业务量级、各模型用量与错误率，可被用于侧信道侦察。
    Prometheus scraper 通过 Authorization 头携带 admin token 抓取。

    返回 ``text/plain; version=0.0.4``，可被 Prometheus scraper 直接抓取。
    包含 ``request_count`` / ``request_latency`` / ``llm_tokens`` / ``llm_cost``。
    """
    return PlainTextResponse(
        metrics.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """根路径。"""
    return {"service": "aiops-console", "docs": "/docs"}


app.include_router(api_router)
