"""Eval Suite — FastAPI 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.domains.evals import service
from app.domains.evals.models import EvalRunCreate, EvalRunOut

router = APIRouter(prefix="/evals", tags=["evals"])


@router.get("", response_model=list[EvalRunOut])
async def list_evals(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[EvalRunOut]:
    runs = await service.list_evals(session, limit=limit, offset=offset)
    return [EvalRunOut.model_validate(r) for r in runs]


@router.post("", response_model=EvalRunOut, status_code=status.HTTP_201_CREATED)
async def create_eval(
    payload: EvalRunCreate, session: AsyncSession = Depends(get_session)
) -> EvalRunOut:
    run = await service.create_eval(session, payload)
    return EvalRunOut.model_validate(run)


@router.get("/{eval_id}", response_model=EvalRunOut)
async def get_eval(
    eval_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> EvalRunOut:
    run = await service.get_eval(session, eval_id)
    return EvalRunOut.model_validate(run)


@router.post("/{eval_id}/run", response_model=EvalRunOut)
async def run_eval(
    eval_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> EvalRunOut:
    """同步执行 eval。predict_fn 为 None 时用 case.expected 自比对。"""
    run = await service.run_eval(session, eval_id)
    return EvalRunOut.model_validate(run)
