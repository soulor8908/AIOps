"""Eval Suite — FastAPI 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.domains.auth.models import User
from app.domains.evals import service
from app.domains.evals.models import (
    EvalRunCreate,
    EvalRunOut,
    EvalSampleCreate,
    EvalSampleOut,
    OnlineEvalRequest,
)

router = APIRouter(prefix="/evals", tags=["evals"])


@router.get("", response_model=list[EvalRunOut])
async def list_evals(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[EvalRunOut]:
    runs = await service.list_evals(session, limit=limit, offset=offset)
    return [EvalRunOut.model_validate(r) for r in runs]


@router.post("", response_model=EvalRunOut, status_code=status.HTTP_201_CREATED)
async def create_eval(
    payload: EvalRunCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> EvalRunOut:
    run = await service.create_eval(session, payload)
    return EvalRunOut.model_validate(run)


# ===================== P0-3: Online eval 闭环 =====================
# 注意：静态路径必须在 /{eval_id} 之前注册，否则会被路径参数拦截。
# FastAPI 路由按注册顺序匹配，/{eval_id} 会消费任意单段路径。


@router.get("/samples", response_model=list[EvalSampleOut])
async def list_samples(
    judged: bool | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    priority_min: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[EvalSampleOut]:
    """列生产采样样本，支持按 judged / agent_id / priority_min 过滤。

    C5：结果按 ``priority DESC, sampled_at DESC`` 排序，高优先级样本在前。
    """
    samples = await service.list_samples(
        session, judged=judged, agent_id=agent_id,
        priority_min=priority_min, limit=limit, offset=offset,
    )
    return [EvalSampleOut.model_validate(s) for s in samples]


@router.post(
    "/samples",
    response_model=EvalSampleOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_sample(
    payload: EvalSampleCreate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> EvalSampleOut:
    """手动录入采样样本（生产自动采样由 execute_agent 钩子完成）。

    P4-4：改 admin-only——手动录样影响 eval 数据质量,仅 admin 可操作。
    """
    sample = await service.record_sample(session, payload)
    return EvalSampleOut.model_validate(sample)


@router.post("/online-eval", response_model=EvalRunOut)
async def run_online_eval(
    payload: OnlineEvalRequest,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> EvalRunOut:
    """触发 online eval 闭环：取样本 → 匹配离线 golden → LLM judge → 写 EvalRun。

    同步执行（与 ``POST /evals/{id}/run`` 一致）。生产批量评估建议在低峰期
    或异步任务中调用，避免阻塞请求线程。

    P4-4：改 admin-only——online eval 批量调用 LLM 产生成本,仅 admin 可触发。
    """
    run = await service.run_online_eval(session, payload)
    return EvalRunOut.model_validate(run)


@router.get("/{eval_id}", response_model=EvalRunOut)
async def get_eval(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> EvalRunOut:
    run = await service.get_eval(session, eval_id)
    return EvalRunOut.model_validate(run)


@router.post("/{eval_id}/run", response_model=EvalRunOut)
async def run_eval(
    eval_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> EvalRunOut:
    """同步执行 eval。predict_fn 为 None 时用 case.expected 自比对。

    P4-4：改 admin-only——执行 eval 批量调用 LLM 产生成本,仅 admin 可触发。
    """
    run = await service.run_eval(session, eval_id)
    return EvalRunOut.model_validate(run)
