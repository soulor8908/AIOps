"""Agent autonomous loop 测试（P0-2）。

覆盖三层：
1. 纯函数：``_compute_next_run`` 解析 interval、``AgentCreate.schedule`` 校验
2. 调度编排：``tick`` / ``_execute_one`` 用 mock service + mock AsyncSessionLocal，
   验证到期查询、并发执行、超时/失败隔离、状态标记、metrics 采集
3. API 契约：POST /agents 带 schedule 字段创建 + 非法 schedule 422

不依赖真实 LLM / DB（orchestration 层全 mock）。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.metrics import metrics
from app.domains.agents import scheduler
from app.domains.agents.models import Agent, AgentCreate, ExecutionResult

# ===================== 辅助：构造 Agent / Fake Session =====================


def _agent(
    *,
    name: str = "scheduled-agent",
    schedule: str | None = "interval:60",
    schedule_enabled: bool = True,
    next_run_at: datetime | None = None,
) -> Agent:
    """构造内存 Agent（不入库），带 schedule 字段。"""
    return Agent(
        id=uuid.uuid4(),
        name=name,
        model_alias="default",
        tools=[],
        max_turns=3,
        temperature=0.7,
        is_active=True,
        schedule=schedule,
        schedule_enabled=schedule_enabled,
        next_run_at=next_run_at or datetime.now(UTC),
    )


class _FakeSession:
    """最小 async session mock：``get`` 返回预设 agent，``commit``/``flush`` no-op。"""

    def __init__(self, agents: dict[uuid.UUID, Agent]) -> None:
        self._agents = agents
        self.committed = False
        self.flushed = False

    async def get(self, cls: Any, pk: Any) -> Agent | None:  # noqa: ARG002
        return self._agents.get(pk)

    async def commit(self) -> None:
        self.committed = True

    async def flush(self) -> None:
        self.flushed = True


def _fake_session_local(agents: dict[uuid.UUID, Agent]) -> Any:
    """构造可替代 ``AsyncSessionLocal`` 的 callable：每次调用返回 async ctx mgr。"""

    def _factory() -> Any:
        session = _FakeSession(agents)

        @asynccontextmanager
        async def _ctx() -> Any:
            yield session

        return _ctx()

    return _factory


# ===================== 1. 纯函数：_compute_next_run =====================


def test_compute_next_run_interval() -> None:
    """interval:<seconds> 解析为 now + seconds。"""
    now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    nxt = scheduler.service._compute_next_run("interval:300", now)
    assert nxt == now + timedelta(seconds=300)


def test_compute_next_run_none_schedule_returns_none() -> None:
    """schedule 为 None 时返回 None。"""
    assert scheduler.service._compute_next_run(None, datetime.now(UTC)) is None


def test_compute_next_run_invalid_format_returns_none() -> None:
    """非法格式（无 interval: 前缀）返回 None，不抛异常。"""
    now = datetime.now(UTC)
    assert scheduler.service._compute_next_run("cron:0 * * * *", now) is None
    assert scheduler.service._compute_next_run("garbage", now) is None


def test_compute_next_run_non_integer_seconds_returns_none() -> None:
    """seconds 非整数返回 None。"""
    assert scheduler.service._compute_next_run(
        "interval:abc", datetime.now(UTC)
    ) is None


def test_compute_next_run_non_positive_seconds_returns_none() -> None:
    """seconds ≤ 0 返回 None。"""
    now = datetime.now(UTC)
    assert scheduler.service._compute_next_run("interval:0", now) is None
    assert scheduler.service._compute_next_run("interval:-5", now) is None


# ===================== 1b. AgentCreate.schedule 校验 =====================


def test_agent_create_schedule_valid() -> None:
    """合法 interval 格式通过校验。"""
    payload = AgentCreate(name="a", schedule="interval:600", schedule_enabled=True)
    assert payload.schedule == "interval:600"
    assert payload.schedule_enabled is True


def test_agent_create_schedule_none_ok() -> None:
    """schedule 默认 None（无调度）。"""
    payload = AgentCreate(name="a")
    assert payload.schedule is None
    assert payload.schedule_enabled is False


def test_agent_create_schedule_empty_string_becomes_none() -> None:
    """空字符串归一化为 None。"""
    payload = AgentCreate(name="a", schedule="")
    assert payload.schedule is None


def test_agent_create_schedule_invalid_format_rejected() -> None:
    """非 interval: 前缀被拒。"""
    with pytest.raises(ValueError, match="interval"):
        AgentCreate(name="a", schedule="cron:0 * * * *")


def test_agent_create_schedule_non_integer_rejected() -> None:
    """seconds 非整数被拒。"""
    with pytest.raises(ValueError, match="整数"):
        AgentCreate(name="a", schedule="interval:abc")


def test_agent_create_schedule_non_positive_rejected() -> None:
    """seconds ≤ 0 被拒。"""
    with pytest.raises(ValueError, match="正整数"):
        AgentCreate(name="a", schedule="interval:0")
    with pytest.raises(ValueError, match="正整数"):
        AgentCreate(name="a", schedule="interval:-5")


# ===================== 2. 调度编排：tick / _execute_one =====================


async def test_tick_no_due_agents_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无到期 agent 时 tick 返回 0，不执行任何 agent。"""
    monkeypatch.setattr(
        scheduler.service, "list_due_agents", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(scheduler.service, "mark_agent_run_started", AsyncMock())
    monkeypatch.setattr(scheduler.service, "execute_agent", AsyncMock())
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local({}))

    n = await scheduler.tick()
    assert n == 0
    scheduler.service.execute_agent.assert_not_called()


async def test_tick_executes_due_agents_and_marks_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有到期 agent 时：标记 started → 并发执行 → 标记 finished。"""
    a1 = _agent(name="a1")
    a2 = _agent(name="a2")
    agents_map = {a1.id: a1, a2.id: a2}

    started_calls: list[Any] = []
    finished_calls: list[Any] = []

    async def _started(session: Any, agent: Agent, now: datetime) -> None:  # noqa: ARG001
        started_calls.append(agent.id)

    async def _finished(
        session: Any,  # noqa: ARG001
        agent: Agent,
        *,
        status: str,
        now: datetime,  # noqa: ARG001
        error: str | None = None,
    ) -> None:
        finished_calls.append((agent.id, status, error))

    async def _exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        return ExecutionResult(final_answer="ok", success=True)

    monkeypatch.setattr(
        scheduler.service, "list_due_agents", AsyncMock(return_value=[a1, a2])
    )
    monkeypatch.setattr(scheduler.service, "mark_agent_run_started", _started)
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", _finished)
    monkeypatch.setattr(scheduler.service, "execute_agent", _exec)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local(agents_map))

    n = await scheduler.tick()
    assert n == 2
    # 两个 agent 都被标记 started
    assert sorted(started_calls) == sorted([a1.id, a2.id])
    # 两个 agent 都执行了
    assert sorted(f[0] for f in finished_calls) == sorted([a1.id, a2.id])
    # 成功状态
    assert all(f[1] == "success" for f in finished_calls)


async def test_tick_error_isolation_single_failure_doesnt_block_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单 agent 执行抛异常不阻塞其他 agent（gather return_exceptions）。"""
    a_ok = _agent(name="ok")
    a_bad = _agent(name="bad")
    agents_map = {a_ok.id: a_ok, a_bad.id: a_bad}

    async def _exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        if agent_id == a_bad.id:
            raise RuntimeError("boom")
        return ExecutionResult(final_answer="ok", success=True)

    finished: list[tuple[uuid.UUID, str]] = []

    async def _finished(
        session: Any,  # noqa: ARG001
        agent: Agent,
        *,
        status: str,
        now: datetime,  # noqa: ARG001
        error: str | None = None,
    ) -> None:
        finished.append((agent.id, status))

    monkeypatch.setattr(
        scheduler.service, "list_due_agents", AsyncMock(return_value=[a_ok, a_bad])
    )
    monkeypatch.setattr(scheduler.service, "mark_agent_run_started", AsyncMock())
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", _finished)
    monkeypatch.setattr(scheduler.service, "execute_agent", _exec)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local(agents_map))

    n = await scheduler.tick()
    assert n == 2  # 两个都被选中执行
    status_map = dict(finished)
    assert status_map[a_ok.id] == "success"
    assert status_map[a_bad.id] == "failed"


async def test_execute_one_success_records_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_execute_one 成功时记 agent_runs{success} + agent_run_duration。"""
    metrics.reset()
    agent = _agent(name="ok-agent")
    agents_map = {agent.id: agent}

    async def _exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        return ExecutionResult(final_answer="done", success=True)

    monkeypatch.setattr(scheduler.service, "execute_agent", _exec)
    finished_mock = AsyncMock()
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", finished_mock)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local(agents_map))

    try:
        await scheduler._execute_one(agent)
        # mark_agent_run_finished 被调用，status=success
        assert finished_mock.await_count == 1
        call_kwargs = finished_mock.await_args.kwargs
        assert call_kwargs["status"] == "success"
        assert call_kwargs["error"] is None
        # metrics 记录
        assert metrics.get_counter("agent_runs", ("ok-agent", "success")) == 1.0
        assert metrics.get_counter("agent_runs", ("ok-agent", "failed")) == 0.0
    finally:
        metrics.reset()


async def test_execute_one_timeout_marks_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_execute_one 超时时 status=timeout。"""
    metrics.reset()
    agent = _agent(name="slow-agent")
    agents_map = {agent.id: agent}

    async def _slow_exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        await asyncio.sleep(10)
        return ExecutionResult(final_answer="never", success=True)

    monkeypatch.setattr(scheduler.service, "execute_agent", _slow_exec)
    monkeypatch.setattr(
        scheduler.settings, "agent_scheduler_timeout_seconds", 0.05
    )
    finished_mock = AsyncMock()
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", finished_mock)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local(agents_map))

    try:
        await scheduler._execute_one(agent)
        assert finished_mock.await_count == 1
        call_kwargs = finished_mock.await_args.kwargs
        assert call_kwargs["status"] == "timeout"
        assert "timeout" in (call_kwargs["error"] or "")
        assert metrics.get_counter("agent_runs", ("slow-agent", "timeout")) == 1.0
    finally:
        metrics.reset()


async def test_execute_one_failure_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_execute_one 执行抛异常时 status=failed，error 含异常类名。"""
    metrics.reset()
    agent = _agent(name="fail-agent")
    agents_map = {agent.id: agent}

    async def _fail_exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr(scheduler.service, "execute_agent", _fail_exec)
    finished_mock = AsyncMock()
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", finished_mock)
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local(agents_map))

    try:
        await scheduler._execute_one(agent)
        assert finished_mock.await_count == 1
        call_kwargs = finished_mock.await_args.kwargs
        assert call_kwargs["status"] == "failed"
        assert "RuntimeError" in (call_kwargs["error"] or "")
        assert "LLM exploded" in (call_kwargs["error"] or "")
        assert metrics.get_counter("agent_runs", ("fail-agent", "failed")) == 1.0
    finally:
        metrics.reset()


async def test_execute_one_agent_deleted_marks_nothing_but_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent 在执行后被删除（session.get 返回 None）：跳过 mark_finished，仍记 metrics。"""
    metrics.reset()
    agent = _agent(name="gone-agent")

    async def _exec(
        session: Any, agent_id: uuid.UUID, request: Any  # noqa: ARG001
    ) -> ExecutionResult:
        return ExecutionResult(final_answer="ok", success=True)

    monkeypatch.setattr(scheduler.service, "execute_agent", _exec)
    finished_mock = AsyncMock()
    monkeypatch.setattr(scheduler.service, "mark_agent_run_finished", finished_mock)
    # agents_map 为空 → session.get 返回 None
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", _fake_session_local({}))

    try:
        await scheduler._execute_one(agent)
        # agent 已不存在，不调用 mark_finished
        assert finished_mock.await_count == 0
        # 但 metrics 仍记录（执行本身发生了）
        assert metrics.get_counter("agent_runs", ("gone-agent", "success")) == 1.0
    finally:
        metrics.reset()


# ===================== 3. API 契约：schedule 字段 =====================


def test_create_agent_with_schedule_returns_schedule_fields(client: Any) -> None:
    """POST /agents 带 schedule 创建，响应含 schedule 字段且 next_run_at 已设。"""
    resp = client.post(
        "/api/v1/agents",
        json={
            "name": "cron-agent",
            "schedule": "interval:120",
            "schedule_enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule"] == "interval:120"
    assert body["schedule_enabled"] is True
    # schedule_enabled=True 时 next_run_at 立即设为当前时间附近（worker 首轮即执行）
    assert body["next_run_at"] is not None
    assert body["last_run_status"] is None
    assert body["last_run_at"] is None


def test_create_agent_without_schedule_has_nulls(client: Any) -> None:
    """POST /agents 不带 schedule，响应中 schedule/next_run_at 为 null。"""
    resp = client.post("/api/v1/agents", json={"name": "plain-agent"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule"] is None
    assert body["schedule_enabled"] is False
    assert body["next_run_at"] is None


def test_create_agent_invalid_schedule_returns_422(client: Any) -> None:
    """非法 schedule 格式返回 422 校验错误。"""
    resp = client.post(
        "/api/v1/agents",
        json={"name": "bad", "schedule": "cron:0 * * * *"},
    )
    assert resp.status_code == 422
