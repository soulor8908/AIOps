"""Conversation Analytics — 单元测试。

覆盖 service 纯函数：list / get / dashboard。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.exceptions import NotFoundError
from app.domains.analytics.models import Conversation, Message
from app.domains.analytics import service


@pytest_asyncio.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, model_alias: str = "gpt-4o-mini") -> Conversation:
    conv = Conversation(
        model_alias=model_alias,
        title="t1",
        total_tokens=100,
        total_cost=Decimal("0.01"),
    )
    session.add(conv)
    await session.flush()
    session.add_all([
        Message(conversation_id=conv.id, role="user", content="hi", tokens_in=5),
        Message(
            conversation_id=conv.id,
            role="assistant",
            content="hello",
            tokens_out=10,
            latency_ms=200,
        ),
    ])
    await session.flush()
    return conv


@pytest.mark.asyncio
async def test_list_conversations(session: AsyncSession) -> None:
    await _seed(session)
    convs = await service.list_conversations(session)
    assert len(convs) == 1
    assert len(convs[0].messages) == 2


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
