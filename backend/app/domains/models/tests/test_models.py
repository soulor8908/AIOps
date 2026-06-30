"""Model Router — 单元测试。

覆盖 service 纯函数（CRUD + cost 计算 + route 策略）。
chat_completion 走 LLMClient，测试用 stub。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.exceptions import NotFoundError
from app.core.llm_client import LLMResponse
from app.domains.models.models import (
    ChatRequest,
    ChatMessage,
    ModelConfigCreate,
    ModelProvider,
    ModelConfigUpdate,
    RoutingStrategy,
)
from app.domains.models import service


@pytest_asyncio.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_get_model(session: AsyncSession) -> None:
    created = await service.create_model(
        session,
        ModelConfigCreate(
            alias="gpt-4o",
            provider=ModelProvider.OPENAI,
            model_name="gpt-4o",
            priority=10,
        ),
    )
    assert created.alias == "gpt-4o"
    fetched = await service.get_model(session, "gpt-4o")
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_get_model_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await service.get_model(session, "nope")


@pytest.mark.asyncio
async def test_list_models_orders_by_priority(session: AsyncSession) -> None:
    await service.create_model(
        session,
        ModelConfigCreate(alias="a", model_name="m-a", priority=50),
    )
    await service.create_model(
        session,
        ModelConfigCreate(alias="b", model_name="m-b", priority=10),
    )
    models = await service.list_models(session)
    assert models[0].alias == "b"
    assert models[1].alias == "a"


@pytest.mark.asyncio
async def test_update_model(session: AsyncSession) -> None:
    await service.create_model(
        session, ModelConfigCreate(alias="u", model_name="m1", priority=10)
    )
    updated = await service.update_model(
        session, "u", ModelConfigUpdate(model_name="m2", is_active=False)
    )
    assert updated.model_name == "m2"
    assert updated.is_active is False


@pytest.mark.asyncio
async def test_delete_model(session: AsyncSession) -> None:
    await service.create_model(session, ModelConfigCreate(alias="d", model_name="m"))
    await service.delete_model(session, "d")
    with pytest.raises(NotFoundError):
        await service.get_model(session, "d")


@pytest.mark.asyncio
async def test_route_model_direct_returns_primary(session: AsyncSession) -> None:
    await service.create_model(
        session, ModelConfigCreate(alias="p", model_name="m", priority=10)
    )
    candidates = await service.route_model(session, "p", RoutingStrategy.DIRECT)
    assert len(candidates) == 1
    assert candidates[0].alias == "p"


@pytest.mark.asyncio
async def test_compute_cost() -> None:
    config = MagicMock()
    config.cost_per_1k_input = Decimal("0.01")
    config.cost_per_1k_output = Decimal("0.03")
    cost = service._compute_cost(config, {"prompt_tokens": 1000, "completion_tokens": 500})
    # 0.01 + 0.015 = 0.025
    assert cost == Decimal("0.025000")


@pytest.mark.asyncio
async def test_chat_completion_uses_llm(session: AsyncSession) -> None:
    await service.create_model(
        session,
        ModelConfigCreate(
            alias="chat-model",
            provider=ModelProvider.OPENAI,
            model_name="gpt-4o-mini",
            api_key_env="OPENAI_API_KEY",
        ),
    )
    fake_client = MagicMock()
    fake_client.chat = AsyncMock(
        return_value=LLMResponse(
            content="hello back",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
    )
    fake_client.close = AsyncMock()
    with patch("app.domains.models.service.LLMClient", return_value=fake_client):
        resp = await service.chat_completion(
            session,
            "chat-model",
            ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
        )
    assert resp.content == "hello back"
    assert resp.fallback_used is False
