"""Conversation Analytics — 业务逻辑纯函数。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.domains.analytics.models import (
    Conversation,
    DashboardMetrics,
    Message,
)


async def list_conversations(
    session: AsyncSession,
    user_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Conversation]:
    """列出对话，可选按 user_id 过滤。"""
    stmt = select(Conversation).options(selectinload(Conversation.messages))
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    stmt = stmt.order_by(Conversation.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation:
    """获取对话（含 messages）。"""
    stmt = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id)
    )
    conv = (await session.execute(stmt)).scalar_one_or_none()
    if conv is None:
        raise NotFoundError(f"对话 {conversation_id} 不存在")
    return conv


async def get_dashboard_metrics(
    session: AsyncSession, days: int = 7
) -> DashboardMetrics:
    """聚合仪表盘指标。默认统计最近 7 天。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total_conv = await _scalar(
        session, select(func.count()).select_from(Conversation)
    )
    total_msg = await _scalar(session, select(func.count()).select_from(Message))
    total_tokens = await _scalar(
        session,
        select(func.coalesce(func.sum(Conversation.total_tokens), 0)).select_from(Conversation),
    )
    total_cost = await _scalar(
        session,
        select(func.coalesce(func.sum(Conversation.total_cost), Decimal("0"))).select_from(Conversation),
    )

    avg_msgs = float(total_msg) / float(total_conv) if total_conv else 0.0
    avg_latency = await _avg_latency(session)

    active_models = await _active_models(session)
    conv_7d = await _conversations_by_day(session, since)

    return DashboardMetrics(
        total_conversations=int(total_conv),
        total_messages=int(total_msg),
        total_tokens=int(total_tokens),
        total_cost=Decimal(str(total_cost)),
        avg_messages_per_conversation=round(avg_msgs, 2),
        avg_latency_ms=round(avg_latency, 2),
        active_models=active_models,
        conversations_last_7d=conv_7d,
    )


async def _scalar(session: AsyncSession, stmt: Any) -> Any:
    return (await session.execute(stmt)).scalar_one()


async def _avg_latency(session: AsyncSession) -> float:
    stmt = select(func.avg(Message.latency_ms)).where(Message.latency_ms.is_not(None))
    val = (await session.execute(stmt)).scalar_one()
    return float(val) if val is not None else 0.0


async def _active_models(session: AsyncSession) -> list[dict[str, Any]]:
    """活跃模型排行（按 token 总量降序）。"""
    stmt = (
        select(
            Conversation.model_alias,
            func.sum(Conversation.total_tokens).label("tokens"),
            func.count().label("conversations"),
        )
        .where(Conversation.model_alias.is_not(None))
        .group_by(Conversation.model_alias)
        .order_by(func.sum(Conversation.total_tokens).desc())
        .limit(10)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {"model": row.model_alias, "tokens": int(row.tokens or 0), "conversations": int(row.conversations)}
        for row in rows
    ]


async def _conversations_by_day(
    session: AsyncSession, since: datetime
) -> list[dict[str, Any]]:
    """按天聚合对话数（最近 N 天）。"""
    stmt = (
        select(
            func.date_trunc("day", Conversation.created_at).label("day"),
            func.count().label("count"),
            func.sum(Conversation.total_tokens).label("tokens"),
        )
        .where(Conversation.created_at >= since)
        .group_by("day")
        .order_by("day")
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            # row.day 在 PG 为 datetime、SQLite UDF 为字符串；统一取前 10 字符 YYYY-MM-DD
            "date": str(row.day)[:10] if row.day is not None else "",
            "count": int(row.count),
            "tokens": int(row.tokens or 0),
        }
        for row in rows
    ]


__all__ = ["get_conversation", "get_dashboard_metrics", "list_conversations"]
