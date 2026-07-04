"""Conversation Analytics — 单元测试。

覆盖 service 纯函数：list / get / dashboard。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.exceptions import NotFoundError
from app.domains.analytics import service
from app.domains.analytics.models import Conversation, Message


@pytest_asyncio.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(
    session: AsyncSession,
    model_alias: str = "gpt-4o-mini",
    created_at: datetime | None = None,
) -> Conversation:
    conv = Conversation(
        model_alias=model_alias,
        title="t1",
        total_tokens=100,
        total_cost=Decimal("0.01"),
    )
    if created_at is not None:
        conv.created_at = created_at
    session.add(conv)
    await session.flush()
    msgs = [
        Message(conversation_id=conv.id, role="user", content="hi", tokens_in=5),
        Message(
            conversation_id=conv.id,
            role="assistant",
            content="hello",
            tokens_out=10,
            latency_ms=200,
        ),
    ]
    # 窗口过滤测试需让 messages 的 created_at 与 conversation 对齐，
    # 否则 server_default=now() 会让旧对话的消息仍落在今天。
    if created_at is not None:
        for m in msgs:
            m.created_at = created_at
    session.add_all(msgs)
    await session.flush()
    return conv


@pytest.mark.asyncio
async def test_list_conversations(session: AsyncSession) -> None:
    await _seed(session)
    convs = await service.list_conversations(session)
    assert len(convs) == 1
    assert len(convs[0].messages) == 2


@pytest.mark.asyncio
async def test_list_conversations_date_range(session: AsyncSession) -> None:
    """P3：created_at 闭区间过滤（start_date / end_date 含当日全天）。"""
    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    await _seed(session, created_at=now)  # 今天
    await _seed(session, model_alias="old", created_at=old)  # 10 天前

    # 只取最近 1 天：含 today，排除 10 天前
    today = now.date()
    convs = await service.list_conversations(session, start_date=today)
    assert len(convs) == 1
    assert convs[0].model_alias == "gpt-4o-mini"

    # 只取 10 天前那天
    convs_old = await service.list_conversations(
        session, start_date=old.date(), end_date=old.date()
    )
    assert len(convs_old) == 1
    assert convs_old[0].model_alias == "old"

    # 区间覆盖两者
    convs_all = await service.list_conversations(
        session, start_date=old.date(), end_date=today
    )
    assert len(convs_all) == 2


@pytest.mark.asyncio
async def test_get_conversation(session: AsyncSession) -> None:
    conv = await _seed(session)
    fetched = await service.get_conversation(session, conv.id)
    assert fetched.id == conv.id
    assert len(fetched.messages) == 2
    assert fetched.messages[0].role == "user"


@pytest.mark.asyncio
async def test_get_conversation_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await service.get_conversation(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_dashboard_metrics(session: AsyncSession) -> None:
    await _seed(session)
    await _seed(session, model_alias="claude-3.5")
    metrics = await service.get_dashboard_metrics(session, days=7)
    assert metrics.total_conversations == 2
    assert metrics.total_messages == 4
    assert metrics.total_tokens == 200
    assert metrics.avg_messages_per_conversation == 2.0
    assert len(metrics.active_models) == 2


@pytest.mark.asyncio
async def test_dashboard_empty(session: AsyncSession) -> None:
    metrics = await service.get_dashboard_metrics(session)
    assert metrics.total_conversations == 0
    assert metrics.avg_latency_ms == 0.0


@pytest.mark.asyncio
async def test_dashboard_respects_days_window(session: AsyncSession) -> None:
    """P1：所有聚合按 days 窗口过滤，窗口外的数据不计入 total_*。"""
    now = datetime.now(UTC)
    await _seed(session, created_at=now)  # 今天
    await _seed(session, model_alias="old", created_at=now - timedelta(days=10))  # 10 天前

    metrics = await service.get_dashboard_metrics(session, days=7)
    # 仅今天的 1 条对话在窗口内（10 天前被排除）
    assert metrics.total_conversations == 1
    assert metrics.total_messages == 2
    assert len(metrics.active_models) == 1
    assert metrics.active_models[0]["model"] == "gpt-4o-mini"


# ===================== P2-9 AI 系统健康度 =====================


@pytest.mark.asyncio
async def test_get_ai_health_metrics_empty(session: AsyncSession) -> None:
    """无调用记录时健康度全 0（除 tool_call_success_rate 默认 1.0）。"""
    from app.core.metrics import metrics as registry

    registry.reset()
    try:
        health = await service.get_ai_health_metrics(session)
        assert health.llm_error_rate == 0.0
        assert health.tool_call_success_rate == 1.0
        assert health.avg_latency_ms == 0.0
        assert health.active_model_count == 0
        assert health.total_llm_calls == 0
        assert health.total_llm_errors == 0
    finally:
        registry.reset()


@pytest.mark.asyncio
async def test_get_ai_health_metrics_error_rate(session: AsyncSession) -> None:
    """错误率 = llm_errors / (llm_errors + llm_calls)，活跃模型数合并两者。"""
    from app.core.metrics import metrics as registry

    registry.reset()
    try:
        # gpt-4o: 4 成功 + 1 错误
        for _ in range(4):
            registry.record_llm_call("gpt-4o")
        registry.record_llm_error("gpt-4o", "non_retryable")
        # claude: 仅 1 错误（无成功调用，但仍计入活跃模型）
        registry.record_llm_error("claude", "retryable_exhausted")

        health = await service.get_ai_health_metrics(session)
        # 总计 4 calls + 2 errors = 6，error_rate = 2/6 ≈ 0.3333
        assert health.total_llm_calls == 4
        assert health.total_llm_errors == 2
        assert abs(health.llm_error_rate - round(2 / 6, 4)) < 1e-6
        # 活跃模型：gpt-4o + claude
        assert health.active_model_count == 2
    finally:
        registry.reset()
