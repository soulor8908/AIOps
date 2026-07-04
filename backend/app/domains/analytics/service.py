"""Conversation Analytics — 业务逻辑纯函数。"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

import redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.core.metrics import metrics
from app.core.redis import get_redis
from app.domains.analytics.models import (
    AIHealthMetrics,
    Conversation,
    DashboardMetrics,
    Message,
)

logger = logging.getLogger("app.analytics.service")

# Dashboard 指标 Redis 缓存（P3）。dashboard 容忍轻微延迟，60s TTL 平衡
# 实时性与 DB 负载。缓存失败不阻断请求（降级到 DB 直查）。
_DASHBOARD_CACHE_TTL = 60


async def list_conversations(
    session: AsyncSession,
    user_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Conversation]:
    """列出对话，可选按 user_id 与 created_at 区间过滤。

    ``start_date`` / ``end_date`` 为闭区间（含当日全天）：
    ``start_date`` 取当日 00:00:00，``end_date`` 取次日 00:00:00 作为排他上界，
    复用 ``idx_conversations_created`` 索引。
    """
    stmt = select(Conversation).options(selectinload(Conversation.messages))
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    if start_date is not None:
        stmt = stmt.where(
            Conversation.created_at >= datetime.combine(start_date, time.min)
        )
    if end_date is not None:
        # 排他上界：含 end_date 当日全天
        upper = datetime.combine(end_date + timedelta(days=1), time.min)
        stmt = stmt.where(Conversation.created_at < upper)
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
    """聚合仪表盘指标，统计最近 ``days`` 天（默认 7 天）。

    所有聚合（total_* / active_models / conversations_last_7d）均按 ``since``
    时间窗口过滤，确保指标自洽——避免 total_conversations 与
    conversations_last_7d 不一致误导用户。

    P3：Redis 缓存 60s TTL + 查询合并（原 6 次独立聚合 → 3 次）。
    """
    cache_key = f"dashboard:metrics:{days}"

    # 尝试读缓存（Redis 不可用时降级到 DB，不阻断请求）
    try:
        cached = await get_redis().get(cache_key)
        if cached is not None:
            return DashboardMetrics.model_validate_json(cached)
    except redis.RedisError:
        logger.warning("dashboard cache read failed, falling back to DB")

    since = datetime.now(UTC) - timedelta(days=days)

    # P3：合并 conversations 的 count + sum(tokens) + sum(cost) 为单条查询（原 3 次 → 1 次）
    conv_stmt = (
        select(
            func.count(),
            func.coalesce(func.sum(Conversation.total_tokens), 0),
            func.coalesce(func.sum(Conversation.total_cost), Decimal("0")),
        )
        .select_from(Conversation)
        .where(Conversation.created_at >= since)
    )
    total_conv, total_tokens, total_cost = (await session.execute(conv_stmt)).one()

    # P3：合并 messages 的 count + avg(latency) 为单条查询（原 2 次 → 1 次）
    msg_stmt = (
        select(
            func.count(),
            func.avg(Message.latency_ms),
        )
        .select_from(Message)
        .where(Message.created_at >= since)
    )
    total_msg, avg_latency_val = (await session.execute(msg_stmt)).one()

    avg_msgs = float(total_msg) / float(total_conv) if total_conv else 0.0
    avg_latency = float(avg_latency_val) if avg_latency_val is not None else 0.0

    active_models = await _active_models(session, since)
    conv_7d = await _conversations_by_day(session, since)

    result = DashboardMetrics(
        total_conversations=int(total_conv),
        total_messages=int(total_msg),
        total_tokens=int(total_tokens),
        total_cost=Decimal(str(total_cost)),
        avg_messages_per_conversation=round(avg_msgs, 2),
        avg_latency_ms=round(avg_latency, 2),
        active_models=active_models,
        conversations_last_7d=conv_7d,
    )

    # 写缓存（失败不影响响应）
    try:
        await get_redis().set(
            cache_key, result.model_dump_json(), ex=_DASHBOARD_CACHE_TTL
        )
    except redis.RedisError:
        logger.warning("dashboard cache write failed")

    return result


async def _active_models(
    session: AsyncSession, since: datetime
) -> list[dict[str, Any]]:
    """活跃模型排行（按 token 总量降序），仅统计 ``since`` 之后。"""
    stmt = (
        select(
            Conversation.model_alias,
            func.sum(Conversation.total_tokens).label("tokens"),
            func.count().label("conversations"),
        )
        .where(
            Conversation.model_alias.is_not(None),
            Conversation.created_at >= since,
        )
        .group_by(Conversation.model_alias)
        .order_by(func.sum(Conversation.total_tokens).desc())
        .limit(10)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "model": row.model_alias,
            "tokens": int(row.tokens or 0),
            "conversations": int(row.conversations),
        }
        for row in rows
    ]


async def _conversations_by_day(
    session: AsyncSession, since: datetime
) -> list[dict[str, Any]]:
    """按天聚合对话数与成本（最近 N 天）。"""
    stmt = (
        select(
            func.date_trunc("day", Conversation.created_at).label("day"),
            func.count().label("count"),
            func.sum(Conversation.total_tokens).label("tokens"),
            func.coalesce(func.sum(Conversation.total_cost), Decimal("0")).label("cost"),
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
            "count": int(cast(Any, row.count)),
            "tokens": int(row.tokens or 0),
            "cost": str(row.cost or Decimal("0")),
        }
        for row in rows
    ]


async def get_ai_health_metrics(session: AsyncSession) -> AIHealthMetrics:
    """返回 AI 系统健康度（P2-9）。

    区别于 ``get_dashboard_metrics``（业务指标：token/成本/对话数），本函数返回
    AI 系统本身的运维健康度，从进程内 ``metrics`` 注册表读取累计值：

    - ``llm_error_rate``: ``llm_errors`` / (``llm_errors`` + ``llm_calls``)；
      无调用记录时为 0.0（避免除零）。
    - ``tool_call_success_rate``: 1 - ``tool_errors`` / ``tool_calls``；
      无工具调用时为 1.0（无失败证据即视为健康）。
    - ``avg_ttft_ms``: 从 ``llm_ttft`` histogram 读取平均首 token 延迟。
    - ``avg_latency_ms``: 从 ``messages.latency_ms`` 全量平均估算（含 user/assistant
      消息），与 TTFT 互补反映总延迟。
    - ``active_model_count``: 有 ``llm_calls`` 或 ``llm_errors`` 记录的 distinct
      模型数（一个模型只要被尝试调用即视为活跃）。
    - ``total_llm_calls`` / ``total_llm_errors``: 跨所有 model 的累计计数。
    - ``failure_mode_clusters``: ``tool_errors`` 按 (tool_name, error_type) → count
      降序 top 5，失败模式聚类帮助定位高频失败工具与错误类型。

    注：``metrics`` 为进程内注册表，多 worker 部署时每个 worker 独立计数，
    本函数返回当前 worker 视角的健康度；Prometheus scraper 会汇总。
    """
    total_calls = int(metrics.get_counter_sum("llm_calls"))
    total_errors = int(metrics.get_counter_sum("llm_errors"))

    if total_calls + total_errors > 0:
        llm_error_rate = total_errors / (total_calls + total_errors)
    else:
        llm_error_rate = 0.0

    # 活跃模型数：合并 llm_calls 与 llm_errors 的 distinct model label。
    # llm_calls 标签为 (model,)，llm_errors 标签为 (model, error_type)，取首元素。
    active_models: set[str] = set()
    for labels in metrics.get_counter_label_values("llm_calls"):
        if labels:
            active_models.add(labels[0])
    for labels in metrics.get_counter_label_values("llm_errors"):
        if labels:
            active_models.add(labels[0])

    # P2-9：工具成功率 = 1 - tool_errors / tool_calls
    total_tool_calls = int(metrics.get_counter_sum("tool_calls"))
    total_tool_errors = int(metrics.get_counter_sum("tool_errors"))
    if total_tool_calls > 0:
        tool_call_success_rate = 1.0 - (total_tool_errors / total_tool_calls)
    else:
        tool_call_success_rate = 1.0

    # P2-9：TTFT 平均值，从 llm_ttft histogram 读取
    avg_ttft = metrics.get_histogram_avg("llm_ttft")

    # P2-9：失败模式聚类 — tool_errors top 5 (tool_name, error_type) → count
    failure_clusters_raw = metrics.get_counter_top_labels("tool_errors", top_n=5)
    failure_mode_clusters = [
        {
            "tool_name": labels[0] if len(labels) > 0 else "",
            "error_type": labels[1] if len(labels) > 1 else "",
            "count": int(count),
        }
        for labels, count in failure_clusters_raw
    ]

    # LLM 平均延迟：从 messages.latency_ms 全量平均估算。
    # 不加时间窗口（健康度关注当前累计态；窗口由 dashboard 负责）。
    latency_stmt = select(func.avg(Message.latency_ms))
    avg_latency_val = (await session.execute(latency_stmt)).scalar()
    avg_latency = float(avg_latency_val) if avg_latency_val is not None else 0.0

    return AIHealthMetrics(
        llm_error_rate=round(llm_error_rate, 4),
        tool_call_success_rate=round(tool_call_success_rate, 4),
        avg_ttft_ms=round(avg_ttft, 2),
        avg_latency_ms=round(avg_latency, 2),
        active_model_count=len(active_models),
        total_llm_calls=total_calls,
        total_llm_errors=total_errors,
        failure_mode_clusters=failure_mode_clusters,
    )


__all__ = [
    "get_ai_health_metrics",
    "get_conversation",
    "get_dashboard_metrics",
    "list_conversations",
]
