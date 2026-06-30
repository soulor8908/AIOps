"""Prompt Studio — FastAPI 路由。

所有路由挂载在 /api/v1/prompts 前缀下。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.domains.prompts import service
from app.domains.prompts.models import (
    DiffResult,
    PromptCreate,
    PromptOut,
    PromptUpdate,
    PromptVersionCreate,
    PromptVersionOut,
)

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("", response_model=list[PromptOut])
async def list_prompts(
    q: str | None = Query(default=None, description="名称模糊搜索"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[PromptOut]:
    prompts = await service.list_prompts(session, q=q, limit=limit, offset=offset)
    return [service.to_prompt_out(p) for p in prompts]


@router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    payload: PromptCreate, session: AsyncSession = Depends(get_session)
) -> PromptOut:
    prompt = await service.create_prompt(session, payload)
    refreshed = await service.get_prompt(session, prompt.id)
    return service.to_prompt_out(refreshed)


@router.get("/{prompt_id}", response_model=PromptOut)
async def get_prompt(
    prompt_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> PromptOut:
    prompt = await service.get_prompt(session, prompt_id)
    return service.to_prompt_out(prompt)


@router.put("/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: uuid.UUID,
    payload: PromptUpdate,
    session: AsyncSession = Depends(get_session),
) -> PromptOut:
    prompt = await service.update_prompt(session, prompt_id, payload)
    refreshed = await service.get_prompt(session, prompt.id)
    return service.to_prompt_out(refreshed)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    await service.delete_prompt(session, prompt_id)


@router.get("/{prompt_id}/versions", response_model=list[PromptVersionOut])
async def list_versions(
    prompt_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> list[PromptVersionOut]:
    prompt = await service.get_prompt(session, prompt_id)
    return [service.to_version_out(v) for v in prompt.versions]


@router.post(
    "/{prompt_id}/versions",
    response_model=PromptVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    prompt_id: uuid.UUID,
    payload: PromptVersionCreate,
    session: AsyncSession = Depends(get_session),
) -> PromptVersionOut:
    version = await service.create_version(session, prompt_id, payload)
    return service.to_version_out(version)


@router.post(
    "/{prompt_id}/versions/{version_id}/rollback",
    response_model=PromptVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def rollback_version(
    prompt_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> PromptVersionOut:
    version = await service.rollback_prompt(session, prompt_id, version_id)
    return service.to_version_out(version)


@router.get("/{prompt_id}/diff", response_model=DiffResult)
async def diff_versions(
    prompt_id: uuid.UUID,
    frm: int = Query(..., alias="from", ge=1, description="起始版本号"),
    to: int = Query(..., ge=1, description="目标版本号"),
    session: AsyncSession = Depends(get_session),
) -> DiffResult:
    return await service.diff_versions(session, prompt_id, frm, to)
