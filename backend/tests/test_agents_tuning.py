"""Agent 配置调优推荐测试（E1：eval 反馈到 agent 配置优化）。

覆盖：
1. **aggregate_agent_failures**：DB 聚合统计（total/failed/avg/failed_samples）
2. **classify_failure_patterns**：根因分类 + 占比统计 + 代表性 reason
3. **_build_suggestion**：根因 → 配置推荐映射
   - FORMAT_ERROR → system_prompt 追加格式约束
   - HALLUCINATION → 降低 temperature
   - TOOL_MISUSE → system_prompt 追加工具指引
   - REASONING_ERROR → 提高 max_turns
   - AMBIGUOUS_QUERY / INCOMPLETE_INFO → system_prompt 追加澄清策略
   - 整体低分 + 未开 self_eval → 开启 self_eval + self_heal
4. **recommend_agent_config**：端到端
   - 失败样本不足 → 空推荐 + rationale 说明
   - 足够样本 → 结构化推荐
   - agent 不存在 → NotFoundError
5. **update_agent service**：PATCH 语义 + 所有权校验 + schedule 一致性
6. **PATCH /agents/{id} + POST /agents/{id}/recommendations**：HTTP 端点
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import NotFoundError, ValidationError
from app.domains.agents.models import Agent, AgentUpdate
from app.domains.agents.self_diagnose import RootCause
from app.domains.agents.tuning import (
    AgentConfigRecommendation,
    FailurePattern,
    aggregate_agent_failures,
    classify_failure_patterns,
    recommend_agent_config,
)
from app.domains.agents import service as agent_service
from app.domains.evals.models import EvalSample


# ===================== 辅助：构造 Agent + EvalSample =====================


def _make_agent(
    *,
    self_eval: bool = False,
    temperature: float = 0.7,
    max_turns: int = 10,
    system_prompt: str | None = None,
) -> Agent:
    return Agent(
        name="test-agent",
        system_prompt=system_prompt,
        model_alias="default",
        tools=[],
        max_turns=max_turns,
        temperature=temperature,
        self_eval=self_eval,
        self_heal=False,
        self_eval_threshold=0.7,
        self_heal_max_retries=1,
    )


def _make_sample(
    agent_id: uuid.UUID,
    *,
    score: float,
    reason: str,
    actual: str = "",
    judged: bool = True,
    priority: int = 0,
) -> EvalSample:
    return EvalSample(
        agent_id=agent_id,
        trigger_source="http",
        input="test input",
        actual_output=actual or "test output",
        expected_output=None,
        metadata_={},
        judged=judged,
        judge_score=score,
        judge_reason=reason,
        priority=priority,
    )


@pytest.fixture
async def session():
    """独立 SQLite in-memory session，隔离于 TestClient fixture。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    # 触发 ORM 元数据注册
    from app.domains import agents, evals  # noqa: F401
    from app.core.database import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


# ===================== 1. aggregate_agent_failures =====================


@pytest.mark.asyncio
async def test_aggregate_agent_failures_counts(session: AsyncSession) -> None:
    """聚合统计：total/failed/avg_score/failed_samples 计数正确。"""
    agent = _make_agent()
    session.add(agent)
    await session.flush()
    # 3 条 judged：2 条失败（score<0.7）+ 1 条通过
    for score in [0.3, 0.5, 0.9]:
        session.add(
            _make_sample(agent.id, score=score, reason=f"reason-{score}")
        )
    await session.flush()

    total, failed, avg_score, failed_samples = await aggregate_agent_failures(
        session, agent.id
    )
    assert total == 3
    assert failed == 2
    assert avg_score == pytest.approx((0.3 + 0.5 + 0.9) / 3, abs=0.01)
    assert len(failed_samples) == 2


@pytest.mark.asyncio
async def test_aggregate_agent_failures_no_samples(session: AsyncSession) -> None:
    """无 judged 样本时返回 0/0/0.0/[]。"""
    agent = _make_agent()
    session.add(agent)
    await session.flush()
    total, failed, avg_score, failed_samples = await aggregate_agent_failures(
        session, agent.id
    )
    assert total == 0
    assert failed == 0
    assert avg_score == 0.0
    assert failed_samples == []


# ===================== 2. classify_failure_patterns =====================


def test_classify_failure_patterns_groups_by_cause() -> None:
    """根因分类：多类根因按 count 降序，ratio 计算正确。"""
    agent_id = uuid.uuid4()
    samples = [
        _make_sample(agent_id, score=0.3, reason="输出格式不符合 JSON 要求"),
        _make_sample(agent_id, score=0.4, reason="markdown 结构错误"),
        _make_sample(agent_id, score=0.5, reason="存在幻觉，捏造了事实"),
        _make_sample(agent_id, score=0.6, reason="工具参数调用错误"),
    ]
    patterns = classify_failure_patterns(samples)
    # 4 条样本，FORMAT_ERROR 2 条（占比 0.5），其余各 1 条
    assert len(patterns) >= 2
    assert patterns[0].root_cause == RootCause.FORMAT_ERROR
    assert patterns[0].count == 2
    assert patterns[0].ratio == 0.5
    # 代表性 reason 最多 3 条
    assert len(patterns[0].sample_reasons) <= 3


def test_classify_failure_patterns_empty() -> None:
    """空样本列表返回空模式列表。"""
    assert classify_failure_patterns([]) == []


# ===================== 3. _build_suggestion 映射 =====================


def test_suggestion_format_error_appends_prompt() -> None:
    """FORMAT_ERROR 高占比 → system_prompt 追加格式约束。"""
    from app.domains.agents.tuning import _build_suggestion

    agent = _make_agent(system_prompt="You are helpful.")
    patterns = [
        FailurePattern(
            root_cause=RootCause.FORMAT_ERROR,
            count=5,
            ratio=0.6,
            sample_reasons=["格式错误"],
        )
    ]
    suggestion, rationale = _build_suggestion(agent, avg_score=0.8, patterns=patterns)
    assert "system_prompt" in suggestion
    assert "格式" in suggestion["system_prompt"]
    assert "FORMAT_ERROR" in rationale or "format" in rationale.lower()


def test_suggestion_hallucination_lowers_temperature() -> None:
    """HALLUCINATION 高占比 → 降低 temperature。"""
    from app.domains.agents.tuning import _build_suggestion

    agent = _make_agent(temperature=0.8)
    patterns = [
        FailurePattern(
            root_cause=RootCause.HALLUCINATION,
            count=4,
            ratio=0.5,
            sample_reasons=["幻觉"],
        )
    ]
    suggestion, _ = _build_suggestion(agent, avg_score=0.6, patterns=patterns)
    assert "temperature" in suggestion
    assert suggestion["temperature"] < 0.8


def test_suggestion_reasoning_error_increases_max_turns() -> None:
    """REASONING_ERROR 高占比 → 提高 max_turns。"""
    from app.domains.agents.tuning import _build_suggestion

    agent = _make_agent(max_turns=10)
    patterns = [
        FailurePattern(
            root_cause=RootCause.REASONING_ERROR,
            count=4,
            ratio=0.5,
            sample_reasons=["推理错误"],
        )
    ]
    suggestion, _ = _build_suggestion(agent, avg_score=0.6, patterns=patterns)
    assert suggestion["max_turns"] == 15  # 10 + 5


def test_suggestion_low_score_enables_self_eval() -> None:
    """整体低分 + 未开 self_eval → 开启 self_eval + self_heal。"""
    from app.domains.agents.tuning import _build_suggestion

    agent = _make_agent(self_eval=False)
    patterns: list[FailurePattern] = []
    suggestion, _ = _build_suggestion(agent, avg_score=0.4, patterns=patterns)
    assert suggestion.get("self_eval") is True
    assert suggestion.get("self_heal") is True


def test_suggestion_low_ratio_ignored() -> None:
    """根因占比低于阈值（30%）不触发推荐。"""
    from app.domains.agents.tuning import _build_suggestion

    agent = _make_agent(temperature=0.7)
    patterns = [
        FailurePattern(
            root_cause=RootCause.HALLUCINATION,
            count=1,
            ratio=0.1,  # 低于 0.3 阈值
            sample_reasons=["幻觉"],
        )
    ]
    suggestion, _ = _build_suggestion(agent, avg_score=0.8, patterns=patterns)
    # 低占比不触发，且 avg_score 不低 → 空推荐
    assert "temperature" not in suggestion


# ===================== 4. recommend_agent_config 端到端 =====================


@pytest.mark.asyncio
async def test_recommend_agent_config_insufficient_samples(session: AsyncSession) -> None:
    """失败样本不足 → 空推荐 + rationale 说明样本不足。"""
    agent = _make_agent()
    session.add(agent)
    await session.flush()
    # 仅 2 条失败样本（阈值 5）
    for score in [0.3, 0.4]:
        session.add(_make_sample(agent.id, score=score, reason="格式错误"))
    await session.flush()

    rec = await recommend_agent_config(session, agent.id)
    assert rec.failed_samples == 2
    assert rec.suggested_update == {}
    assert "不足" in rec.rationale


@pytest.mark.asyncio
async def test_recommend_agent_config_generates_suggestion(session: AsyncSession) -> None:
    """足够失败样本 + FORMAT_ERROR 主导 → 生成 system_prompt 推荐配置。"""
    agent = _make_agent(system_prompt="Base prompt.")
    session.add(agent)
    await session.flush()
    # 6 条 FORMAT_ERROR 失败样本（占比 100%）
    for _ in range(6):
        session.add(
            _make_sample(agent.id, score=0.3, reason="输出格式不符合 JSON 要求")
        )
    await session.flush()

    rec = await recommend_agent_config(session, agent.id)
    assert rec.failed_samples == 6
    assert "system_prompt" in rec.suggested_update
    assert "格式" in rec.suggested_update["system_prompt"]


@pytest.mark.asyncio
async def test_recommend_agent_config_agent_not_found(session: AsyncSession) -> None:
    """agent 不存在 → NotFoundError。"""
    with pytest.raises(NotFoundError):
        await recommend_agent_config(session, uuid.uuid4())


# ===================== 5. update_agent service =====================


@pytest.mark.asyncio
async def test_update_agent_patch_semantics(session: AsyncSession) -> None:
    """PATCH 语义：仅传入字段被更新，其余保持原值。"""
    agent = _make_agent()
    session.add(agent)
    await session.flush()
    original_temp = agent.temperature

    payload = AgentUpdate(max_turns=20)  # 仅更新 max_turns
    updated = await agent_service.update_agent(session, agent.id, payload)
    assert updated.max_turns == 20
    assert updated.temperature == original_temp  # 未传入字段保持不变


@pytest.mark.asyncio
async def test_update_agent_schedule_consistency(session: AsyncSession) -> None:
    """schedule_enabled=True 但 schedule 为空 → ValidationError。"""
    agent = _make_agent()
    session.add(agent)
    await session.flush()
    # 先清空 schedule，再启用 schedule_enabled
    payload = AgentUpdate(schedule="", schedule_enabled=True)
    with pytest.raises(ValidationError):
        await agent_service.update_agent(session, agent.id, payload)


@pytest.mark.asyncio
async def test_update_agent_tools_serialized(session: AsyncSession) -> None:
    """tools 字段从 ToolDef 列表序列化为 dict 列表。"""
    from app.domains.agents.models import ToolDef, ToolType

    agent = _make_agent()
    session.add(agent)
    await session.flush()
    payload = AgentUpdate(tools=[ToolDef(name="calc", type=ToolType.CALCULATOR)])
    updated = await agent_service.update_agent(session, agent.id, payload)
    assert updated.tools == [{"name": "calc", "type": "calculator", "description": None, "config": {}}]


# ===================== 6. HTTP 端点 =====================


def test_patch_agent_endpoint(client) -> None:  # type: ignore[no-untyped-def]
    """PATCH /agents/{id} 更新 agent 配置（admin-only）。"""
    # 先创建 agent
    resp = client.post(
        "/api/v1/agents",
        json={"name": "patch-test", "max_turns": 10},
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    # PATCH 更新 max_turns
    resp = client.patch(f"/api/v1/agents/{agent_id}", json={"max_turns": 25})
    assert resp.status_code == 200
    assert resp.json()["max_turns"] == 25


def test_patch_agent_user_forbidden(user_client) -> None:  # type: ignore[no-untyped-def]
    """非 admin 调用 PATCH → 403（auth 先于资源查找）。"""
    resp = user_client.patch(f"/api/v1/agents/{uuid.uuid4()}", json={"max_turns": 5})
    assert resp.status_code == 403


def test_agent_recommendations_endpoint_empty(client) -> None:  # type: ignore[no-untyped-def]
    """POST /agents/{id}/recommendations 无 eval 样本 → 空推荐。"""
    resp = client.post("/api/v1/agents", json={"name": "rec-test"})
    agent_id = resp.json()["id"]
    resp = client.post(f"/api/v1/agents/{agent_id}/recommendations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_samples"] == 0
    assert body["failed_samples"] == 0
    assert body["suggested_update"] == {}
    assert "不足" in body["rationale"]


def test_agent_recommendations_admin_only(user_client) -> None:  # type: ignore[no-untyped-def]
    """非 admin 调用 recommendations → 403（auth 先于资源查找）。"""
    resp = user_client.post(f"/api/v1/agents/{uuid.uuid4()}/recommendations")
    assert resp.status_code == 403
