"""Conversation Analytics — FastAPI 路由。"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.domains.analytics import service
from app.domains.analytics.models import AIHealthMetrics, ConversationOut, DashboardMetrics
from app.domains.auth.models import User

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    start_date: date | None = Query(
        default=None, description="起始日 YYYY-MM-DD（含当日）"
    ),
    end_date: date | None = Query(
        default=None, description="结束日 YYYY-MM-DD（含当日）"
    ),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ConversationOut]:
    # P4-3：非 admin 强制只能查自己 user_id,防越权查看他人对话。
    # admin 可传任意 user_id 或不传(查全部)用于运营分析。
    effective_user_id = user_id if current_user.is_admin else current_user.id
    convs = await service.list_conversations(
        session,
        user_id=effective_user_id,
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
    )
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


@router.get("/ai-health", response_model=AIHealthMetrics)
async def ai_health(
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(get_current_admin),
) -> AIHealthMetrics:
    """AI 系统健康度（P2-9）。

    需 admin 权限——与 ``/metrics`` 一致：错误率/调用量等运维指标含敏感信息，
    匿名暴露会泄露系统稳定性信号，可被用于侧信道侦察（如定向压测已知高错误率模型）。
    """
    return await service.get_ai_health_metrics(session)
