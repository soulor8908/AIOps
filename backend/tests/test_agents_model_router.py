"""成本感知模型路由（P1-6）测试 — 复杂度路由 + token budget 熔断。

覆盖：
1. **classify_complexity** 纯函数：简单/复杂/中等判定
2. **BudgetTracker**：滑动窗口、consume、remaining、is_exhausted、budget=0 不限
3. **ModelRouter**：复杂度映射、熔断降级、record_usage
4. **execute_agent 集成**（mock）：启用路由后覆盖 model_alias
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.llm_client import LLMConfig
from app.domains.agents.model_router import (
    BudgetTracker,
    ComplexityLevel,
    ModelRouter,
    classify_complexity,
)
from app.domains.agents.models import Agent, ExecutionResult
from app.main import app

# ===================== 1. classify_complexity =====================


def test_classify_short_input_no_tools_is_simple() -> None:
    assert classify_complexity("你好", None) == ComplexityLevel.SIMPLE
    assert classify_complexity("hi", []) == ComplexityLevel.SIMPLE


def test_classify_long_input_is_complex() -> None:
    long_input = "x" * 2500
    assert classify_complexity(long_input, None) == ComplexityLevel.COMPLEX


def test_classify_code_keywords_is_complex() -> None:
    assert classify_complexity("请写一个 def foo(): 的函数", None) == ComplexityLevel.COMPLEX
    assert classify_complexity("帮我 ```python 代码", None) == ComplexityLevel.COMPLEX


def test_classify_many_tools_is_complex() -> None:
    tools = [{"type": f"t{i}"} for i in range(3)]
    assert classify_complexity("普通问题", tools) == ComplexityLevel.COMPLEX


def test_classify_moderate() -> None:
    # 中等长度输入、无工具、无代码关键词
    assert classify_complexity("a" * 500, None) == ComplexityLevel.MODERATE


def test_classify_empty_input_is_simple() -> None:
    assert classify_complexity("", None) == ComplexityLevel.SIMPLE


# ===================== 2. BudgetTracker =====================


def test_budget_consume_and_remaining() -> None:
    bt = BudgetTracker(budget=1000, window_seconds=60)
    assert bt.remaining() == 1000
    bt.consume(300)
    assert bt.remaining() == 700
    bt.consume(200)
    assert bt.remaining() == 500


def test_budget_is_exhausted_when_remaining_zero() -> None:
    bt = BudgetTracker(budget=100, window_seconds=60)
    assert bt.is_exhausted() is False
    bt.consume(100)
    assert bt.is_exhausted() is True


def test_budget_zero_means_unlimited() -> None:
    bt = BudgetTracker(budget=0, window_seconds=60)
    bt.consume(999999)
    assert bt.is_exhausted() is False
    assert bt.remaining() == 0  # 0 budget，但永不熔断


def test_budget_sliding_window_evicts_expired() -> None:
    bt = BudgetTracker(budget=1000, window_seconds=10)
    now = 1000.0
    bt.consume(800, now=now)
    assert bt.remaining(now=now) == 200
    # 窗口外（now + 11s）应滑出
    assert bt.remaining(now=now + 11) == 1000
    assert bt.is_exhausted(now=now + 11) is False


def test_budget_consume_zero_or_negative_noop() -> None:
    bt = BudgetTracker(budget=100, window_seconds=60)
    bt.consume(0)
    bt.consume(-5)
    assert bt.remaining() == 100


# ===================== 3. ModelRouter =====================


def test_router_routes_simple_to_cheap() -> None:
    router = ModelRouter(
        cheap_alias="cheap", default_alias="default", premium_alias="premium"
    )
    alias, complexity, broken = router.route("hi", None)
    assert alias == "cheap"
    assert complexity == ComplexityLevel.SIMPLE
    assert broken is False


def test_router_routes_complex_to_premium() -> None:
    router = ModelRouter(
        cheap_alias="cheap", default_alias="default", premium_alias="premium"
    )
    alias, complexity, broken = router.route("x" * 3000, None)
    assert alias == "premium"
    assert complexity == ComplexityLevel.COMPLEX
    assert broken is False


def test_router_routes_moderate_to_default() -> None:
    router = ModelRouter(
        cheap_alias="cheap", default_alias="default", premium_alias="premium"
    )
    alias, complexity, broken = router.route("a" * 500, None)
    assert alias == "default"
    assert complexity == ComplexityLevel.MODERATE
    assert broken is False


def test_router_circuit_breaker_downgrades_to_cheap() -> None:
    budget = BudgetTracker(budget=100, window_seconds=60)
    budget.consume(100)  # 耗尽
    router = ModelRouter(
        cheap_alias="cheap",
        default_alias="default",
        premium_alias="premium",
        budget=budget,
    )
    # 即使是复杂任务，熔断后也降级到 cheap
    alias, complexity, broken = router.route("x" * 3000, None)
    assert alias == "cheap"
    assert complexity == ComplexityLevel.COMPLEX
    assert broken is True


def test_router_record_usage_consumes_budget() -> None:
    budget = BudgetTracker(budget=1000, window_seconds=60)
    router = ModelRouter(
        cheap_alias="cheap",
        default_alias="default",
        premium_alias="premium",
        budget=budget,
    )
    router.record_usage(400)
    assert budget.remaining() == 600
    router.record_usage(0)  # 零用量不记录
    assert budget.remaining() == 600


def test_router_no_budget_never_breaks() -> None:
    router = ModelRouter(
        cheap_alias="cheap", default_alias="default", premium_alias="premium"
    )
    router.record_usage(999999)
    alias, _, broken = router.route("hi", None)
    assert broken is False


# ===================== 4. execute_agent 集成（mock） =====================


def _setup_mocks(
    monkeypatch: pytest.MonkeyPatch, agent: Agent
) -> dict[str, Any]:
    from app.domains.agents import service as agent_service

    monkeypatch.setattr(agent_service, "get_agent", AsyncMock(return_value=agent))
    captured: dict[str, Any] = {}

    async def _fake_build_llm_config(
        session: Any, model_alias: str, temperature: float | None = None
    ) -> LLMConfig:
        captured["routed_alias"] = model_alias
        return LLMConfig(provider="openai", model=model_alias, api_key="k")

    monkeypatch.setattr(agent_service, "_build_llm_config", _fake_build_llm_config)
    mock_llm = MagicMock()
    mock_llm.close = AsyncMock()
    monkeypatch.setattr(agent_service, "LLMClient", lambda cfg: mock_llm)

    def _spy_executor(*args: Any, **kwargs: Any) -> Any:
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id,
                final_answer="ok",
                success=True,
                total_tokens=50,
            )
        )
        return mock_executor

    monkeypatch.setattr(agent_service, "AgentExecutor", _spy_executor)
    return captured


def _run(client: TestClient, scenario: Any) -> None:
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


def test_execute_agent_cost_routing_disabled_keeps_original_alias(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由关闭时 model_alias 不变。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_cost_routing_enabled", False)
    agent = Agent(
        id=uuid.uuid4(), name="t", system_prompt="x", model_alias="my-model",
        tools=[], max_turns=1, temperature=0.7, is_active=True,
    )
    captured = _setup_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    assert captured["routed_alias"] == "my-model"


def test_execute_agent_cost_routing_enabled_routes_simple_to_cheap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由启用 + 简单输入 → cheap alias。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    # 重置 budget 单例避免跨测试污染
    monkeypatch.setattr(agent_service, "_budget_tracker", None)
    monkeypatch.setattr(agent_service.settings, "agent_cost_routing_enabled", True)
    monkeypatch.setattr(agent_service.settings, "agent_cost_cheap_model_alias", "gpt-4o-mini")
    monkeypatch.setattr(agent_service.settings, "agent_cost_premium_model_alias", "gpt-4o")
    monkeypatch.setattr(agent_service.settings, "agent_cost_token_budget", 0)  # 不限制

    agent = Agent(
        id=uuid.uuid4(), name="t", system_prompt="x", model_alias="my-model",
        tools=[], max_turns=1, temperature=0.7, is_active=True,
    )
    captured = _setup_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="hi")  # 简单输入
        )

    _run(client, _scenario)
    assert captured["routed_alias"] == "gpt-4o-mini"


def test_execute_agent_cost_routing_complex_to_premium(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由启用 + 复杂输入 → premium alias。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service, "_budget_tracker", None)
    monkeypatch.setattr(agent_service.settings, "agent_cost_routing_enabled", True)
    monkeypatch.setattr(agent_service.settings, "agent_cost_cheap_model_alias", "mini")
    monkeypatch.setattr(agent_service.settings, "agent_cost_premium_model_alias", "gpt-4o")
    monkeypatch.setattr(agent_service.settings, "agent_cost_token_budget", 0)

    agent = Agent(
        id=uuid.uuid4(), name="t", system_prompt="x", model_alias="my-model",
        tools=[], max_turns=1, temperature=0.7, is_active=True,
    )
    captured = _setup_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="x" * 3000)  # 复杂输入
        )

    _run(client, _scenario)
    assert captured["routed_alias"] == "gpt-4o"
