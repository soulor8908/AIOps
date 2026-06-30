"""AIOps Console — FastAPI 入口（< 100 行）。

职责：
- lifespan：启动时建表（开发期），关闭连接池
- CORS 中间件
- 注册全局异常处理器
- 挂载聚合路由
- /health 健康检查
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import api_router
from app.core.config import settings
from app.core.database import engine, init_db
from app.core.exceptions import AppError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动建表，关闭 engine。"""
    if settings.debug:
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
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """全局应用异常 → 统一 JSON。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error_code,
            "message": exc.message,
            "detail": exc.detail,
        },
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok", "version": settings.app_version}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """根路径。"""
    return {"service": "aiops-console", "docs": "/docs"}


app.include_router(api_router)
