"""Analytics 聚合与过滤 eval（analytics/SPEC.md Success Criteria）。

覆盖 6 项验收：
1. 列表支持按 user_id 过滤，并预加载 messages
2. dashboard 正确聚合 total_conversations / total_messages / total_tokens / total_cost
3. avg_messages_per_conversation = total_messages / total_conversations（空集为 0）
4. 活跃模型按 token 总量降序取 top 10
5. conversations_last_7d 按天聚合 count 与 tokens
6. avg_latency_ms 仅对非空 latency_ms 求平均

通过 ``client`` fixture 获得独立 SQLite in-memory DB，经 session_factory 直接 seed
Conversation / Message 数据，再调用 service 层断言聚合结果。绕过 HTTP 层，专注聚合逻辑。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.domains.analytics import service as analytics_service
from app.domains.analytics.models import Conversation, Message
from app.main import app

# ===================== 辅助：经 session_factory 执行异步场景 =====================


def _run(
    client: TestClient, scenario: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。

    ``scenario`` 接收一个 AsyncSession，负责 seed + 断言。session_factory 由
    conftest 的 ``_override_get_session`` 提供，``async for`` 退出时自动 commit。
    """
    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


def _conv(
    *,
    user_id: uuid.UUID | None = None,
    model_alias: str = "gpt-4o",
    total_tokens: int = 100,
    total_cost: str = "0.50",
    created_at: datetime | None = None,
) -> Conversation:
    """构造 Conversation（不入库）。"""
    return Conversation(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_id=None,
        model_alias=model_alias,
        title="t",
        metadata_={},
        total_tokens=total_tokens,
        total_cost=Decimal(total_cost),
        created_at=created_at or datetime.now(UTC),
    )


def _msg(
    conv_id: uuid.UUID,
    *,
    role: str = "user",
    content: str = "hi",
    tokens_in: int = 10,
    tokens_out: int = 10,
    latency_ms: int | None = 100,
    created_at: datetime | None = None,
) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        role=role,
        content=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        model_alias="gpt-4o",
        created_at=created_at or datetime.now(UTC),
    )


# ===================== 1. 列表按 user_id 过滤 + 预加载 messages =====================


def test_list_conversations_filter_by_user_and_preload_messages(
    client: TestClient,
) -> None:
    """list_conversations 支持 user_id 过滤且预加载 messages（SPEC 1）。"""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    async def _scenario(session: AsyncSession) -> None:
        conv_a1 = _conv(user_id=user_a)
        conv_a2 = _conv(user_id=user_a)
        conv_b1 = _conv(user_id=user_b)
        # 给 a1 挂 2 条消息，a2 挂 1 条
        session.add_all([
            conv_a1, conv_a2, conv_b1,
            _msg(conv_a1.id), _msg(conv_a1.id, role="assistant"),
            _msg(conv_a2.id),
        ])
        await session.flush()

        # 过滤 user_a → 仅返回 a1, a2
        convs = await analytics_service.list_conversations(session, user_id=user_a)
        assert len(convs) == 2
        assert {c.id for c in convs} == {conv_a1.id, conv_a2.id}

        # selectinload 预加载 messages（access 不触发 lazy load / MissingGreenlet）
        conv_a1_loaded = next(c for c in convs if c.id == conv_a1.id)
        assert len(conv_a1_loaded.messages) == 2
        conv_a2_loaded = next(c for c in convs if c.id == conv_a2.id)
        assert len(conv_a2_loaded.messages) == 1

    _run(client, _scenario)


# ===================== 2. dashboard 聚合四个总量 =====================


def test_dashboard_aggregates_totals(client: TestClient) -> None:
    """dashboard 正确聚合 total_conversations / messages / tokens / cost（SPEC 2）。"""
    async def _scenario(session: AsyncSession) -> None:
        c1 = _conv(model_alias="gpt-4o", total_tokens=100, total_cost="0.50")
        c2 = _conv(model_alias="gpt-4o", total_tokens=200, total_cost="1.00")
        c3 = _conv(model_alias="claude", total_tokens=50, total_cost="0.25")
        session.add_all([
            c1, c2, c3,
            _msg(c1.id), _msg(c1.id, role="assistant"),
            _msg(c2.id),
            _msg(c3.id),
        ])
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=7)

        assert metrics.total_conversations == 3
        assert metrics.total_messages == 4
        assert metrics.total_tokens == 350  # 100+200+50
        assert metrics.total_cost == Decimal("1.750000")

    _run(client, _scenario)


# ===================== 3. avg_messages_per_conversation（含空集为 0） =====================


def test_avg_messages_per_conversation(client: TestClient) -> None:
    """avg_messages_per_conversation = total_messages / total_conversations（SPEC 3）。"""
    async def _scenario(session: AsyncSession) -> None:
        # 3 convs, 4 messages → 4/3 ≈ 1.33
        c1 = _conv()
        c2 = _conv()
        c3 = _conv()
        session.add_all([
            c1, c2, c3,
            _msg(c1.id), _msg(c1.id, role="assistant"),
            _msg(c2.id),
            _msg(c3.id),
        ])
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=7)
        assert metrics.avg_messages_per_conversation == round(4 / 3, 2)

    _run(client, _scenario)


def test_avg_messages_per_conversation_empty_dataset(client: TestClient) -> None:
    """空数据集时 avg_messages_per_conversation 为 0（SPEC 3 空集分支）。"""
    async def _scenario(session: AsyncSession) -> None:
        metrics = await analytics_service.get_dashboard_metrics(session, days=7)
        assert metrics.total_conversations == 0
        assert metrics.avg_messages_per_conversation == 0.0

    _run(client, _scenario)


# ===================== 4. 活跃模型按 token 总量降序 top 10 =====================


def test_active_models_top_10_by_tokens_desc(client: TestClient) -> None:
    """活跃模型按 token 总量降序取 top 10（SPEC 4）。"""
    async def _scenario(session: AsyncSession) -> None:
        # 11 个模型，tokens 从 1100 递减到 100，应只返回前 10
        convs = []
        for i in range(11):
            tokens = 100 * (11 - i)  # 1100, 1000, ..., 100
            convs.append(_conv(model_alias=f"m{i}", total_tokens=tokens))
        session.add_all(convs)
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=90)

        # 仅返回 top 10
        assert len(metrics.active_models) == 10
        # 按 tokens 降序：m0(1100) > m1(1000) > ... > m9(200)，m10(100) 被截断
        tokens_list = [m["tokens"] for m in metrics.active_models]
        assert tokens_list == sorted(tokens_list, reverse=True)
        assert metrics.active_models[0]["model"] == "m0"
        assert metrics.active_models[0]["tokens"] == 1100
        # m10 不在 top 10
        models = {m["model"] for m in metrics.active_models}
        assert "m10" not in models

    _run(client, _scenario)


# ===================== 5. conversations_last_7d 按天聚合 =====================


def test_conversations_by_day_aggregation(client: TestClient) -> None:
    """conversations_last_7d 按天聚合 count 与 tokens（SPEC 5）。"""
    today = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    async def _scenario(session: AsyncSession) -> None:
        # 今天 2 个对话（tokens 100+200=300），昨天 1 个对话（tokens 50）
        session.add_all([
            _conv(total_tokens=100, created_at=today),
            _conv(total_tokens=200, created_at=today),
            _conv(total_tokens=50, created_at=yesterday),
        ])
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=7)

        # 至少包含今天和昨天的聚合
        assert len(metrics.conversations_last_7d) >= 2
        # 按日期升序
        dates = [d["date"] for d in metrics.conversations_last_7d]
        assert dates == sorted(dates)
        # 找到今天和昨天的聚合行
        today_str = today.strftime("%Y-%m-%d")
        yday_str = yesterday.strftime("%Y-%m-%d")
        today_row = next(d for d in metrics.conversations_last_7d if d["date"] == today_str)
        yday_row = next(d for d in metrics.conversations_last_7d if d["date"] == yday_str)
        assert today_row["count"] == 2
        assert today_row["tokens"] == 300
        assert yday_row["count"] == 1
        assert yday_row["tokens"] == 50

    _run(client, _scenario)


# ===================== 6. avg_latency_ms 仅对非空求平均 =====================


def test_avg_latency_excludes_null(client: TestClient) -> None:
    """avg_latency_ms 仅对非空 latency_ms 求平均（SPEC 6）。"""
    async def _scenario(session: AsyncSession) -> None:
        c1 = _conv()
        # 3 条消息：latency 分别为 100, 200, None；avg 应为 (100+200)/2 = 150
        session.add_all([
            c1,
            _msg(c1.id, latency_ms=100),
            _msg(c1.id, latency_ms=200, role="assistant"),
            _msg(c1.id, latency_ms=None, role="system"),
        ])
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=7)
        assert metrics.avg_latency_ms == 150.0

    _run(client, _scenario)


def test_avg_latency_all_null_returns_zero(client: TestClient) -> None:
    """所有 latency_ms 均为 None 时 avg_latency_ms 为 0（SPEC 6 空集分支）。"""
    async def _scenario(session: AsyncSession) -> None:
        c1 = _conv()
        session.add_all([
            c1,
            _msg(c1.id, latency_ms=None),
            _msg(c1.id, latency_ms=None, role="assistant"),
        ])
        await session.flush()

        metrics = await analytics_service.get_dashboard_metrics(session, days=7)
        assert metrics.avg_latency_ms == 0.0

    _run(client, _scenario)
