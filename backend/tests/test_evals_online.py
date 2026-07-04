"""P0-3 Online eval 闭环测试。

覆盖三层：
1. 纯函数：``_match_golden`` 匹配优先级
2. service 编排：record_sample / list_samples / _fetch_golden_cases /
   run_online_eval（exact judge 避免真实 LLM 调用 + 状态流转 + 样本回填 +
   回归检测 + 错误路径）
3. API 契约：POST /evals/samples / GET /evals/samples / POST /evals/online-eval

LLM judge 路径用 mock，避免真实网络调用。exact judge 走纯函数。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.domains.evals import service as eval_service
from app.domains.evals.models import (
    EvalRun,
    EvalSampleCreate,
    EvalStatus,
    JudgeType,
    OnlineEvalRequest,
)
from app.main import app

# ===================== 辅助：经 session_factory 执行异步场景 =====================


def _run(
    client: TestClient, scenario: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。"""
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


async def _seed_golden_run(
    session: AsyncSession,
    name: str = "golden-suite",
    cases: list[dict[str, Any]] | None = None,
    status: str = EvalStatus.PASSED.value,
    score: float = 0.9,
) -> EvalRun:
    """创建一条已完成的 golden EvalRun（供 run_online_eval 匹配）。"""
    if cases is None:
        cases = [
            {"name": "c1", "input": "天气如何", "expected": "晴天"},
            {"name": "c2", "input": "你好", "expected": "你好，有什么可以帮你"},
        ]
    run = EvalRun(
        name=name,
        cases=cases,
        judge_type=JudgeType.EXACT.value,
        status=status,
        score=score,
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        finished_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        pass_count=2,
        fail_count=0,
    )
    session.add(run)
    await session.flush()
    await session.commit()  # 确保 _run 的 break 不丢数据（跨 session 可见）
    return run


# ===================== 1. 纯函数：_match_golden =====================


def test_match_golden_exact_input() -> None:
    """input 完全相等时返回 golden case。"""
    golden = [{"input": "天气", "expected": "晴"}]
    matched = eval_service._match_golden("天气", golden)
    assert matched is not None
    assert matched["expected"] == "晴"


def test_match_golden_normalized_input() -> None:
    """空白/大小写差异时归一化匹配。"""
    golden = [{"input": "Hello   World", "expected": "greeting"}]
    matched = eval_service._match_golden("hello world", golden)
    assert matched is not None
    assert matched["expected"] == "greeting"


def test_match_golden_no_match_returns_none() -> None:
    """无匹配返回 None。"""
    golden = [{"input": "foo", "expected": "bar"}]
    assert eval_service._match_golden("baz", golden) is None


def test_match_golden_empty_golden_returns_none() -> None:
    """golden 为空时返回 None。"""
    assert eval_service._match_golden("anything", []) is None


# ===================== 2. service：record_sample / list_samples =====================


def test_record_sample_persists_fields(client: TestClient) -> None:
    """record_sample 持久化所有字段。"""

    async def _scenario(session: AsyncSession) -> None:
        sample = await eval_service.record_sample(
            session,
            EvalSampleCreate(
                agent_id=None,
                trigger_source="http",
                input="用户问天气",
                actual_output="今天晴",
                metadata={"trace_id": "abc"},
            ),
        )
        assert sample.id is not None
        assert sample.input == "用户问天气"
        assert sample.actual_output == "今天晴"
        assert sample.trigger_source == "http"
        assert sample.judged is False
        assert sample.metadata_ == {"trace_id": "abc"}

    _run(client, _scenario)


def test_list_samples_filter_by_judged(client: TestClient) -> None:
    """list_samples 按 judged 过滤。"""

    async def _scenario(session: AsyncSession) -> None:
        await eval_service.record_sample(
            session, EvalSampleCreate(input="q1", actual_output="a1")
        )
        s2 = await eval_service.record_sample(
            session, EvalSampleCreate(input="q2", actual_output="a2")
        )
        s2.judged = True
        await session.commit()

        unjudged = await eval_service.list_samples(session, judged=False)
        judged = await eval_service.list_samples(session, judged=True)
        assert len(unjudged) == 1
        assert unjudged[0].input == "q1"
        assert len(judged) == 1
        assert judged[0].input == "q2"

    _run(client, _scenario)


# ===================== 3. service：_fetch_golden_cases =====================


def test_fetch_golden_cases_not_found_raises(client: TestClient) -> None:
    """golden run 不存在时抛 NotFoundError。"""

    async def _scenario(session: AsyncSession) -> None:
        with pytest.raises(NotFoundError, match="golden"):
            await eval_service._fetch_golden_cases(session, "nonexistent-suite")

    _run(client, _scenario)


def test_fetch_golden_cases_returns_cases(client: TestClient) -> None:
    """golden run 存在时返回其 cases 快照。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="g1")
        cases = await eval_service._fetch_golden_cases(session, "g1")
        assert len(cases) == 2
        assert cases[0]["input"] == "天气如何"

    _run(client, _scenario)


# ===================== 4. service：run_online_eval（exact judge） =====================


def test_run_online_eval_exact_judge_passes_and_backfills_samples(
    client: TestClient,
) -> None:
    """run_online_eval 用 exact judge：样本匹配 golden → 全过 → PASSED + 样本回填。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="gold-exact")
        # 样本 input 与 golden 完全匹配，actual 与 expected 完全匹配
        s1 = await eval_service.record_sample(
            session,
            EvalSampleCreate(
                input="天气如何", actual_output="晴天", trigger_source="http"
            ),
        )
        s2 = await eval_service.record_sample(
            session,
            EvalSampleCreate(input="你好", actual_output="你好，有什么可以帮你"),
        )

        result = await eval_service.run_online_eval(
            session,
            OnlineEvalRequest(
                golden_run_name="gold-exact",
                judge_type=JudgeType.EXACT,
            ),
        )

        assert result.status == EvalStatus.PASSED.value
        assert result.score == 1.0
        assert result.pass_count == 2
        assert result.fail_count == 0
        assert result.judge_type == JudgeType.EXACT.value
        assert len(result.results) == 2
        # 样本被回填
        await session.refresh(s1)
        await session.refresh(s2)
        assert s1.judged is True
        assert s1.judge_score == 1.0
        assert s1.eval_run_id == result.id
        assert s1.expected_output == "晴天"  # golden 回填
        assert s2.judged is True

    _run(client, _scenario)


def test_run_online_eval_partial_fail_returns_failed(client: TestClient) -> None:
    """样本部分不匹配 golden expected → score < 0.85 → FAILED。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="gold-mix")
        # 全错（actual 与 expected 不匹配）
        await eval_service.record_sample(
            session,
            EvalSampleCreate(input="天气如何", actual_output="不知道"),
        )
        await eval_service.record_sample(
            session,
            EvalSampleCreate(input="你好", actual_output="不知道"),
        )

        result = await eval_service.run_online_eval(
            session,
            OnlineEvalRequest(
                golden_run_name="gold-mix",
                judge_type=JudgeType.EXACT,
            ),
        )

        assert result.status == EvalStatus.FAILED.value
        assert result.score == 0.0
        assert result.fail_count == 2

    _run(client, _scenario)


def test_run_online_eval_specific_sample_ids(client: TestClient) -> None:
    """指定 sample_ids 时只评估这些样本。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="gold-ids")
        s1 = await eval_service.record_sample(
            session,
            EvalSampleCreate(input="天气如何", actual_output="晴天"),
        )
        # 这条不会被评估（不在 sample_ids 中）
        s2 = await eval_service.record_sample(
            session,
            EvalSampleCreate(input="你好", actual_output="错误答案"),
        )

        result = await eval_service.run_online_eval(
            session,
            OnlineEvalRequest(
                sample_ids=[s1.id],
                golden_run_name="gold-ids",
                judge_type=JudgeType.EXACT,
            ),
        )

        assert result.pass_count == 1
        await session.refresh(s2)
        assert s2.judged is False  # 未被评估

    _run(client, _scenario)


def test_run_online_eval_no_samples_raises(client: TestClient) -> None:
    """无可评估样本（无 sample_ids 且无未 judged）时抛 ValidationError。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="gold-empty")
        with pytest.raises(ValidationError, match="无可评估"):
            await eval_service.run_online_eval(
                session,
                OnlineEvalRequest(
                    golden_run_name="gold-empty",
                    judge_type=JudgeType.EXACT,
                ),
            )

    _run(client, _scenario)


def test_run_online_eval_invalid_sample_ids_raises(client: TestClient) -> None:
    """sample_ids 不存在时抛 NotFoundError。"""

    async def _scenario(session: AsyncSession) -> None:
        import uuid as _uuid

        await _seed_golden_run(session, name="gold-bad-ids")
        with pytest.raises(NotFoundError, match="sample_ids"):
            await eval_service.run_online_eval(
                session,
                OnlineEvalRequest(
                    sample_ids=[_uuid.uuid4()],
                    golden_run_name="gold-bad-ids",
                    judge_type=JudgeType.EXACT,
                ),
            )

    _run(client, _scenario)


def test_run_online_eval_regression_detection(client: TestClient) -> None:
    """baseline 高、当前 score 低 → is_regression=True。

    先建一条同名 PASSED run（score=1.0 作为 baseline），再跑一次 score=0.0
    的 online eval，应标记 is_regression=True。
    """

    async def _scenario(session: AsyncSession) -> None:
        # baseline run（同名，先创建）
        await _seed_golden_run(
            session, name="regress-suite", score=1.0, status=EvalStatus.PASSED.value
        )
        # 当前 run：全错 → score=0.0
        await eval_service.record_sample(
            session,
            EvalSampleCreate(input="天气如何", actual_output="错"),
        )

        result = await eval_service.run_online_eval(
            session,
            OnlineEvalRequest(
                golden_run_name="regress-suite",
                judge_type=JudgeType.EXACT,
            ),
        )

        assert result.score == 0.0
        assert result.baseline_score == 1.0
        assert result.is_regression is True

    _run(client, _scenario)


# ===================== 5. service：run_online_eval（LLM judge mock） =====================


def test_run_online_eval_llm_judge_uses_mocked_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM judge 路径：mock client + judge_llm_with_sampling，验证返回 PASSED。"""

    async def _scenario(session: AsyncSession) -> None:
        await _seed_golden_run(session, name="gold-llm")
        await eval_service.record_sample(
            session,
            EvalSampleCreate(input="天气如何", actual_output="晴天"),
        )

        # mock judge_llm_with_sampling 返回高分
        from app.domains.evals.judge import JudgeResult

        async def _fake_judge(*args: Any, **kwargs: Any) -> JudgeResult:
            return JudgeResult(passed=True, score=0.9, reason="good")

        monkeypatch.setattr(
            "app.domains.evals.service.judge_llm_with_sampling", _fake_judge
        )

        # mock LLMClient（避免 _build_llm_client_if_needed 真实创建）
        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        monkeypatch.setattr(
            "app.domains.evals.service._build_llm_client_if_needed",
            lambda judge_type: mock_client,
        )

        result = await eval_service.run_online_eval(
            session,
            OnlineEvalRequest(
                golden_run_name="gold-llm",
                judge_type=JudgeType.LLM,
            ),
        )

        assert result.status == EvalStatus.PASSED.value
        assert result.score == 1.0
        assert result.results[0]["score"] == 0.9

    _run(client, _scenario)


# ===================== 6. API 契约 =====================


def test_api_create_sample_returns_201(client: TestClient) -> None:
    """POST /evals/samples 创建样本返回 201。"""
    resp = client.post(
        "/api/v1/evals/samples",
        json={"input": "测试输入", "actual_output": "测试输出"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["input"] == "测试输入"
    assert body["actual_output"] == "测试输出"
    assert body["judged"] is False
    assert body["trigger_source"] == "http"


def test_api_list_samples_filters(client: TestClient) -> None:
    """GET /evals/samples?judged=false 返回未评估样本。"""
    # 先创建一条
    client.post(
        "/api/v1/evals/samples",
        json={"input": "q1", "actual_output": "a1"},
    )
    resp = client.get("/api/v1/evals/samples", params={"judged": False})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    assert all(s["judged"] is False for s in body)


def test_api_create_sample_rejects_empty_input(client: TestClient) -> None:
    """POST /evals/samples 空 input 返回 422。"""
    resp = client.post(
        "/api/v1/evals/samples",
        json={"input": "", "actual_output": "a"},
    )
    assert resp.status_code == 422


def test_api_run_online_eval_end_to_end(client: TestClient) -> None:
    """POST /evals/online-eval 端到端：先建 golden run，再建样本，触发评估。"""

    # 1. 建 golden run（直接经 service 在测试 DB 中创建）
    def _seed() -> None:
        async def _scenario(session: AsyncSession) -> None:
            await _seed_golden_run(session, name="api-golden")

        _run(client, _scenario)

    _seed()

    # 2. 录入样本（input 与 golden 匹配，actual 正确）
    client.post(
        "/api/v1/evals/samples",
        json={"input": "天气如何", "actual_output": "晴天"},
    )

    # 3. 触发 online eval
    resp = client.post(
        "/api/v1/evals/online-eval",
        json={
            "golden_run_name": "api-golden",
            "judge_type": "exact",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "passed"
    assert body["score"] == 1.0
    assert body["pass_count"] == 1
    assert body["judge_type"] == "exact"


def test_api_run_online_eval_golden_not_found_returns_404(client: TestClient) -> None:
    """golden_run_name 不存在时返回 404。

    需先创建一个样本（否则 run_online_eval 先因无样本抛 ValidationError 422，
    不到 golden 检查）。
    """
    client.post(
        "/api/v1/evals/samples",
        json={"input": "q", "actual_output": "a"},
    )
    resp = client.post(
        "/api/v1/evals/online-eval",
        json={"golden_run_name": "no-such-golden", "judge_type": "exact"},
    )
    assert resp.status_code == 404


# ===================== 7. execute_agent 采样钩子（mock） =====================


def test_execute_agent_sample_hook_fires_when_rate_1(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample_rate=1.0 + 非 scheduled input + success → 触发 record_sample。

    mock rng 强制命中采样，mock AsyncSessionLocal 验证 record_sample 被调。
    """
    import uuid as _uuid

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import Agent, ExecutionResult

    # 强制采样命中
    monkeypatch.setattr(agent_service, "_sample_rng", MagicMock(random=lambda: 0.0))
    monkeypatch.setattr(agent_service.settings, "online_eval_sample_rate", 1.0)

    captured: dict[str, Any] = {}

    class _FakeAsyncCtx:
        async def __aenter__(self) -> AsyncSession:
            self.session = AsyncMock(spec=AsyncSession)
            return self.session

        async def __aexit__(self, *args: Any) -> None:
            pass

    def _fake_session_local() -> _FakeAsyncCtx:
        return _FakeAsyncCtx()

    # mock record_sample 捕获调用
    async def _fake_record(session: Any, payload: Any) -> Any:
        captured["input"] = payload.input
        captured["actual"] = payload.actual_output
        captured["agent_id"] = payload.agent_id
        return MagicMock()

    monkeypatch.setattr(
        "app.domains.evals.service.record_sample", _fake_record
    )
    monkeypatch.setattr(agent_service, "AsyncSessionLocal", _fake_session_local)

    # mock executor.run 返回 success 结果
    agent = Agent(
        id=_uuid.uuid4(),
        name="test",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )

    async def _scenario(session: AsyncSession) -> None:
        # 让 get_agent 返回我们的 agent
        monkeypatch.setattr(
            agent_service, "get_agent", AsyncMock(return_value=agent)
        )
        # mock _build_llm_config
        from app.core.llm_client import LLMConfig

        monkeypatch.setattr(
            agent_service,
            "_build_llm_config",
            AsyncMock(
                return_value=LLMConfig(
                    provider="openai", model="m", api_key="k"
                )
            ),
        )
        # mock LLMClient
        mock_llm = MagicMock()
        mock_llm.close = AsyncMock()
        monkeypatch.setattr(agent_service, "LLMClient", lambda cfg: mock_llm)
        # mock AgentExecutor.run 返回 success
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id,
                final_answer="测试答案",
                success=True,
                total_tokens=10,
            )
        )
        monkeypatch.setattr(
            agent_service, "AgentExecutor", lambda *a, **kw: mock_executor
        )

        from app.domains.agents.models import ExecuteRequest

        result = await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="用户问题")
        )

        assert result.success is True
        # 等待 fire-and-forget task 完成
        import asyncio

        await asyncio.sleep(0.05)

    _run(client, _scenario)

    assert captured.get("input") == "用户问题"
    assert captured.get("actual") == "测试答案"
    assert captured.get("agent_id") == agent.id


def test_execute_agent_skips_sampling_for_scheduled_trigger(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scheduled 触发（input == _SCHEDULED_TRIGGER_INPUT）跳过采样。"""
    import uuid as _uuid
    from unittest.mock import MagicMock

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import Agent, ExecuteRequest, ExecutionResult

    # 即使采样率 1.0 也不应触发
    monkeypatch.setattr(
        agent_service, "_sample_rng", MagicMock(random=lambda: 0.0)
    )
    monkeypatch.setattr(agent_service.settings, "online_eval_sample_rate", 1.0)

    call_count = 0

    async def _counting_record(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return MagicMock()

    monkeypatch.setattr(
        "app.domains.evals.service.record_sample", _counting_record
    )

    agent = Agent(
        id=_uuid.uuid4(),
        name="scheduled",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )

    async def _scenario(session: AsyncSession) -> None:
        monkeypatch.setattr(
            agent_service, "get_agent", AsyncMock(return_value=agent)
        )
        from app.core.llm_client import LLMConfig

        monkeypatch.setattr(
            agent_service,
            "_build_llm_config",
            AsyncMock(
                return_value=LLMConfig(provider="openai", model="m", api_key="k")
            ),
        )
        mock_llm = MagicMock()
        mock_llm.close = AsyncMock()
        monkeypatch.setattr(agent_service, "LLMClient", lambda cfg: mock_llm)
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id,
                final_answer="auto",
                success=True,
                total_tokens=5,
            )
        )
        monkeypatch.setattr(
            agent_service, "AgentExecutor", lambda *a, **kw: mock_executor
        )

        await agent_service.execute_agent(
            session,
            agent.id,
            ExecuteRequest(input=agent_service._SCHEDULED_TRIGGER_INPUT),
        )

    _run(client, _scenario)

    assert call_count == 0


def test_execute_agent_no_sampling_when_rate_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sample_rate=0.0（默认）时不触发采样。"""
    import uuid as _uuid
    from unittest.mock import MagicMock

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import Agent, ExecuteRequest, ExecutionResult

    monkeypatch.setattr(agent_service.settings, "online_eval_sample_rate", 0.0)

    call_count = 0

    async def _counting_record(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return MagicMock()

    monkeypatch.setattr(
        "app.domains.evals.service.record_sample", _counting_record
    )

    agent = Agent(
        id=_uuid.uuid4(),
        name="no-sample",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )

    async def _scenario(session: AsyncSession) -> None:
        monkeypatch.setattr(
            agent_service, "get_agent", AsyncMock(return_value=agent)
        )
        from app.core.llm_client import LLMConfig

        monkeypatch.setattr(
            agent_service,
            "_build_llm_config",
            AsyncMock(
                return_value=LLMConfig(provider="openai", model="m", api_key="k")
            ),
        )
        mock_llm = MagicMock()
        mock_llm.close = AsyncMock()
        monkeypatch.setattr(agent_service, "LLMClient", lambda cfg: mock_llm)
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id,
                final_answer="x",
                success=True,
                total_tokens=1,
            )
        )
        monkeypatch.setattr(
            agent_service, "AgentExecutor", lambda *a, **kw: mock_executor
        )

        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="正常输入")
        )

    _run(client, _scenario)

    assert call_count == 0


# ===================== 8. C5 分层采样 + 优先策略 =====================


def test_compute_sample_priority_heuristics() -> None:
    """C5：_compute_sample_priority 按启发式累加优先级。

    - 短输入 + 无 heal + 无 eval_score → 0
    - 长输入 → +1
    - heal_attempts > 0 → +1
    - eval_score < 0.7 → +1
    - 三者叠加 → 3
    """
    from app.domains.agents.models import ExecutionResult
    from app.domains.agents.service import _compute_sample_priority

    # 0：短输入 + 无 heal + 无 eval_score
    r0 = ExecutionResult(final_answer="ok", success=True)
    assert _compute_sample_priority("短", r0) == 0

    # +1：长输入（超过默认阈值 200）
    long_input = "x" * 250
    r1 = ExecutionResult(final_answer="ok", success=True)
    assert _compute_sample_priority(long_input, r1) == 1

    # +1：heal_attempts > 0
    r2 = ExecutionResult(final_answer="ok", success=True, heal_attempts=2)
    assert _compute_sample_priority("短", r2) == 1

    # +1：eval_score < 0.7
    r3 = ExecutionResult(final_answer="ok", success=True, eval_score=0.3)
    assert _compute_sample_priority("短", r3) == 1

    # eval_score == 0.7 不加分（边界：< 0.7 才加）
    r3b = ExecutionResult(final_answer="ok", success=True, eval_score=0.7)
    assert _compute_sample_priority("短", r3b) == 0

    # +3：三者叠加
    r4 = ExecutionResult(
        final_answer="ok", success=True, heal_attempts=1, eval_score=0.1
    )
    assert _compute_sample_priority(long_input, r4) == 3


def test_record_sample_persists_priority(client: TestClient) -> None:
    """C5：record_sample 持久化 priority 字段。"""

    async def _scenario(session: AsyncSession) -> None:
        sample = await eval_service.record_sample(
            session,
            EvalSampleCreate(
                input="q", actual_output="a", priority=3,
            ),
        )
        assert sample.priority == 3

        # 默认 priority=0
        s2 = await eval_service.record_sample(
            session, EvalSampleCreate(input="q2", actual_output="a2")
        )
        assert s2.priority == 0

    _run(client, _scenario)


def test_list_samples_priority_ordering(client: TestClient) -> None:
    """C5：list_samples 按 priority DESC, sampled_at DESC 排序。"""

    async def _scenario(session: AsyncSession) -> None:
        # 先插低优先级，再插高优先级（sampled_at 自然递增）
        await eval_service.record_sample(
            session, EvalSampleCreate(input="low1", actual_output="a", priority=0)
        )
        await eval_service.record_sample(
            session, EvalSampleCreate(input="high1", actual_output="a", priority=2)
        )
        await eval_service.record_sample(
            session, EvalSampleCreate(input="low2", actual_output="a", priority=0)
        )
        await eval_service.record_sample(
            session, EvalSampleCreate(input="high2", actual_output="a", priority=1)
        )

        samples = await eval_service.list_samples(session)
        # priority DESC：high1(2) > high2(1) > low2(0) ≈ low1(0)
        # 同 priority 内 sampled_at DESC：low2 在 low1 前
        assert samples[0].input == "high1"
        assert samples[1].input == "high2"
        assert samples[2].input == "low2"
        assert samples[3].input == "low1"

    _run(client, _scenario)


def test_list_samples_priority_min_filter(client: TestClient) -> None:
    """C5：list_samples priority_min 过滤。"""

    async def _scenario(session: AsyncSession) -> None:
        await eval_service.record_sample(
            session, EvalSampleCreate(input="p0", actual_output="a", priority=0)
        )
        await eval_service.record_sample(
            session, EvalSampleCreate(input="p1", actual_output="a", priority=1)
        )
        await eval_service.record_sample(
            session, EvalSampleCreate(input="p2", actual_output="a", priority=2)
        )

        high = await eval_service.list_samples(session, priority_min=1)
        assert {s.input for s in high} == {"p1", "p2"}

    _run(client, _scenario)


def test_execute_agent_stratified_sampling_boosts_high_priority(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C5：base_rate=0.1 + 长输入(priority>0) → effective_rate=0.5，采样被 boost。

    mock rng 返回 0.3：base_rate=0.1 时不采样（0.3 > 0.1），但 boost 后
    effective_rate=0.5（0.3 < 0.5）采样命中。验证高优先级请求被分层 boost。
    """
    import uuid as _uuid

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import Agent, ExecuteRequest, ExecutionResult

    # base_rate=0.1, boost=5.0 → effective_rate=0.5 for priority>0
    monkeypatch.setattr(agent_service.settings, "online_eval_sample_rate", 0.1)
    monkeypatch.setattr(agent_service.settings, "online_eval_sample_rate_boost", 5.0)
    # rng=0.3：0.3 > 0.1（base 不命中），0.3 < 0.5（boost 命中）
    monkeypatch.setattr(agent_service, "_sample_rng", MagicMock(random=lambda: 0.3))

    captured: dict[str, Any] = {}

    async def _fake_record(*args: Any, **kwargs: Any) -> Any:
        # record_sample(session, EvalSampleCreate) — priority 在 payload 内
        payload = args[1] if len(args) > 1 else kwargs.get("payload")
        if payload is not None:
            captured["priority"] = getattr(payload, "priority", None)
            captured["input"] = getattr(payload, "input", None)
        return MagicMock()

    monkeypatch.setattr(
        "app.domains.evals.service.record_sample", _fake_record
    )

    class _FakeAsyncCtx:
        async def __aenter__(self) -> AsyncSession:
            self.session = AsyncMock(spec=AsyncSession)
            return self.session

        async def __aexit__(self, *args: Any) -> None:
            pass

    def _fake_session_local() -> _FakeAsyncCtx:
        return _FakeAsyncCtx()

    monkeypatch.setattr(agent_service, "AsyncSessionLocal", _fake_session_local)

    agent = Agent(
        id=_uuid.uuid4(), name="stratified", system_prompt="x",
        model_alias="default", tools=[], max_turns=1, temperature=0.7,
        is_active=True,
    )

    async def _scenario(session: AsyncSession) -> None:
        monkeypatch.setattr(
            agent_service, "get_agent", AsyncMock(return_value=agent)
        )
        from app.core.llm_client import LLMConfig

        monkeypatch.setattr(
            agent_service, "_build_llm_config",
            AsyncMock(return_value=LLMConfig(provider="openai", model="m", api_key="k")),
        )
        mock_llm = MagicMock()
        mock_llm.close = AsyncMock()
        monkeypatch.setattr(agent_service, "LLMClient", lambda cfg: mock_llm)
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id, final_answer="ans", success=True, total_tokens=1,
            )
        )
        monkeypatch.setattr(
            agent_service, "AgentExecutor", lambda *a, **kw: mock_executor
        )

        # 长输入（>200 字符）→ priority=1 → boost 采样
        long_input = "请详细分析" + "x" * 250
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input=long_input)
        )
        import asyncio

        await asyncio.sleep(0.05)

    _run(client, _scenario)

    # 高优先级请求被 boost 采样命中
    assert captured.get("priority") == 1
    assert captured.get("input") is not None


def test_run_online_eval_selects_high_priority_first(
    client: TestClient
) -> None:
    """C5：run_online_eval 无 sample_ids 时按 priority DESC 选取，高优先级先 judge。

    插入 3 条未 judged 样本（priority 0/1/2），golden 匹配全部命中，
    limit=500 全选。拦截 _judge_case 提取 case["input"]，映射回 priority，
    验证 judge 顺序为 priority 2 → 1 → 0。
    """
    judged_inputs: list[str] = []

    async def _scenario(session: AsyncSession) -> None:
        # 三条样本，input 与 golden cases 匹配（exact judge）
        prio_map = {"q0": 0, "q1": 1, "q2": 2}
        for inp, prio in prio_map.items():
            await eval_service.record_sample(
                session,
                EvalSampleCreate(
                    input=inp, actual_output=f"a{inp[1]}", priority=prio,
                ),
            )

        await _seed_golden_run(
            session,
            name="golden-c5",
            cases=[
                {"name": "c0", "input": "q0", "expected": "a0"},
                {"name": "c1", "input": "q1", "expected": "a1"},
                {"name": "c2", "input": "q2", "expected": "a2"},
            ],
        )

        # 拦截 _judge_case 提取 case["input"] 记录评估顺序
        original_judge = eval_service._judge_case

        async def _tracking_judge(*args: Any, **kwargs: Any) -> Any:
            # _judge_case(judge_type, case_dict, actual, llm_client)
            case = args[1] if len(args) > 1 else kwargs.get("case")
            if isinstance(case, dict) and "input" in case:
                judged_inputs.append(case["input"])
            return await original_judge(*args, **kwargs)

        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        mp.setattr(eval_service, "_judge_case", _tracking_judge)
        try:
            await eval_service.run_online_eval(
                session,
                OnlineEvalRequest(
                    golden_run_name="golden-c5",
                    judge_type=JudgeType.EXACT,
                ),
            )
        finally:
            mp.undo()

    _run(client, _scenario)

    # priority DESC 顺序：q2(2) → q1(1) → q0(0)
    assert judged_inputs == ["q2", "q1", "q0"]
