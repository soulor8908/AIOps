"""Conversation Analytics — FastAPI 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.domains.analytics import service
from app.domains.analytics.models import ConversationOut, DashboardMetrics
from app.domains.auth.models import User

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ConversationOut]:
    convs = await service.list_conversations(session, user_id=user_id, limit=limit, offset=offset)
    return [ConversationOut.model_validate(c) for c in convs]


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ConversationOut:
    conv = await service.get_conversation(session, conversation_id)
    return ConversationOut.model_validate(conv)


@router.get("/dashboard", response_model=DashboardMetrics)
async def dashboard(
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DashboardMetrics:
    return await service.get_dashboard_metrics(session, days=days)
