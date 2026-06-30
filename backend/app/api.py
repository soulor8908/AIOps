"""API 路由聚合 — 一行一个 domain。

按领域扁平挂载，所有路由统一在 /api/v1 前缀下（由 main.py include 时设置）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.domains.agents.router import router as agents_router
from app.domains.analytics.router import router as analytics_router
from app.domains.auth.router import router as auth_router
from app.domains.evals.router import router as evals_router
from app.domains.knowledge.router import router as knowledge_router
from app.domains.models.router import router as models_router
from app.domains.prompts.router import router as prompts_router

api_router = APIRouter(prefix="/api/v1")

# 一行一个 domain
api_router.include_router(auth_router)
api_router.include_router(prompts_router)
api_router.include_router(agents_router)
api_router.include_router(knowledge_router)
api_router.include_router(models_router)
api_router.include_router(analytics_router)
api_router.include_router(evals_router)
