"""Model Router — FastAPI 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.domains.auth.models import User
from app.domains.models import service
from app.domains.models.models import (
    ChatRequest,
    ChatResponse,
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[ModelConfigOut])
async def list_models(
    active_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ModelConfigOut]:
    configs = await service.list_models(
        session, active_only=active_only, limit=limit, offset=offset
    )
    return [ModelConfigOut.model_validate(c) for c in configs]


@router.post("", response_model=ModelConfigOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelConfigCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> ModelConfigOut:
    config = await service.create_model(session, payload)
    return ModelConfigOut.model_validate(config)


@router.get("/{alias}", response_model=ModelConfigOut)
async def get_model(
    alias: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ModelConfigOut:
    config = await service.get_model(session, alias)
    return ModelConfigOut.model_validate(config)


@router.put("/{alias}", response_model=ModelConfigOut)
async def update_model(
    alias: str,
    payload: ModelConfigUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> ModelConfigOut:
    config = await service.update_model(session, alias, payload)
    return ModelConfigOut.model_validate(config)


@router.delete("/{alias}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    alias: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> None:
    await service.delete_model(session, alias)


@router.post("/{alias}/chat", response_model=ChatResponse)
async def chat(
    alias: str,
    payload: ChatRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ChatResponse:
    return await service.chat_completion(session, alias, payload)


@router.post("/{alias}/chat/stream")
async def chat_stream(
    alias: str,
    payload: ChatRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """流式聊天补全（P0-1）。SSE 格式，供前端 EventSource 消费。"""
    return StreamingResponse(
        service.stream_chat_completion(session, alias, payload),
        media_type="text/event-stream",
    )
