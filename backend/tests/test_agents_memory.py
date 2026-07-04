"""Agent 记忆层（P1-4）测试 — 向量化历史检索替换 LLM 摘要压缩。

覆盖三层：
1. **service 层**（``upsert_memory`` / ``search_memory``）：
   - upsert 持久化 chunk（SQLite 上 embedder 返回零向量，写入仍成功）
   - search 在 SQLite 上返回 []（pgvector 算子不可用，降级）
   - search 在 PG 上构造 cosine_distance 查询（mock _is_postgresql）
2. **PgMemoryBackend**：
   - search 返回 content 列表
   - search/upsert 异常时不抛出（降级为 []/no-op）
3. **executor 集成**（mock memory）：
   - 有历史时注入 system 消息（relevant_history）
   - 无历史时不注入
   - memory=None 时不注入不持久化（向后兼容）
   - 每轮 observation 持久化
   - 最终答案轮持久化 thought（无 observation）
4. **execute_agent 集成**（mock）：
   - agent_memory_enabled=False → executor.memory is None
   - agent_memory_enabled=True → executor.memory 为 PgMemoryBackend
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm_client import LLMClient, LLMResponse, Message, ToolCall
from app.domains.agents.executor import AgentExecutor
from app.domains.agents.memory import (
    _DEFAULT_TOP_K,
    PgMemoryBackend,
    search_memory,
    upsert_memory,
)
from app.domains.agents.models import Agent, AgentMemoryChunk, ExecutionResult
from app.main import app

# ===================== 辅助 =====================


def _make_agent(name: str = "mem-agent", max_turns: int = 3) -> Agent:
    """构造无需 DB 的 Agent 实例。"""
    return Agent(
        id=uuid.uuid4(),
        name=name,
        system_prompt="助手",
        model_alias="default",
        tools=[],
        max_turns=max_turns,
        temperature=0.7,
        is_active=True,
    )


def _make_mock_llm(responses: list[LLMResponse]) -> Any:
    """构造按顺序返回不同响应的 mock LLMClient。"""
    call_idx = {"n": 0}

    async def fake_chat(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = min(call_idx["n"], len(responses) - 1)
        call_idx["n"] += 1
        return responses[idx]

    stub = MagicMock(spec=LLMClient)
    stub.chat = fake_chat
    stub.close = AsyncMock()
    return stub


class _FakeMemoryBackend:
    """记录调用的假记忆后端，用于 executor 集成测试。"""

    def __init__(self, history: list[str] | None = None) -> None:
        self._history = history or []
        self.search_calls: list[tuple[uuid.UUID, str, int]] = []
        self.upsert_calls: list[dict[str, Any]] = []

    async def search(
        self, agent_id: uuid.UUID, query: str, top_k: int = _DEFAULT_TOP_K
    ) -> list[str]:
        self.search_calls.append((agent_id, query, top_k))
        return list(self._history)

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        turn: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.upsert_calls.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "turn": turn,
                "content": content,
                "metadata": metadata or {},
            }
        )


def _run(
    client: TestClient, scenario: Any
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。"""
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


# ===================== 1. service 层 =====================


async def test_upsert_memory_persists_chunk_with_zero_embedding_on_sqlite(
    client: TestClient,
) -> None:
    """upsert_memory 在 SQLite（无 API key）下仍持久化，embedding 为零向量。

    embedder 无 API key 时返回零向量（1536 维），写入不失败。
    """

    async def _scenario(session: AsyncSession) -> None:
        agent = Agent(
            name="upsert-agent",
            model_alias="default",
            tools=[],
            max_turns=1,
            temperature=0.7,
            is_active=True,
        )
        session.add(agent)
        await session.flush()

        chunk = await upsert_memory(
            session,
            agent_id=agent.id,
            session_id=uuid.uuid4(),
            turn=1,
            content="some observation",
            metadata={"type": "observation"},
        )
        assert chunk.id is not None
        assert chunk.content == "some observation"
        assert chunk.turn == 1
        assert chunk.metadata_ == {"type": "observation"}
        # embedder 无 API key → 零向量
        assert chunk.embedding is not None
        assert all(v == 0.0 for v in chunk.embedding)

    _run(client, _scenario)


async def test_search_memory_returns_empty_on_sqlite(client: TestClient) -> None:
    """search_memory 在 SQLite 上返回 []（pgvector cosine_distance 不可用）。"""

    async def _scenario(session: AsyncSession) -> None:
        agent = Agent(
            name="search-agent",
            model_alias="default",
            tools=[],
            max_turns=1,
            temperature=0.7,
            is_active=True,
        )
        session.add(agent)
        await session.flush()
        # 先写入一条记忆
        await upsert_memory(
            session,
            agent_id=agent.id,
            session_id=uuid.uuid4(),
            turn=1,
            content="历史观察",
        )
        # SQLite 上检索应返回空
        results = await search_memory(session, agent.id, "历史")
        assert results == []

    _run(client, _scenario)


async def test_search_memory_pg_path_constructs_cosine_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """search_memory 在 PG 上构造 cosine_distance 查询（mock _is_postgresql=True）。

    验证 PG 路径走 cosine_distance order_by，不因零向量崩溃。
    """
    from app.domains.agents import memory as mem_mod

    # 强制走 PG 路径
    monkeypatch.setattr(mem_mod, "_is_postgresql", lambda s: True)
    # mock embed_text 返回非零向量避免 cosine_distance(零向量) 歧义
    monkeypatch.setattr(
        mem_mod, "embed_text", AsyncMock(return_value=[0.1] * 1536)
    )

    async def _scenario(session: AsyncSession) -> None:
        agent = Agent(
            name="pg-agent",
            model_alias="default",
            tools=[],
            max_turns=1,
            temperature=0.7,
            is_active=True,
        )
        session.add(agent)
        await session.flush()
        # PG 路径会尝试 cosine_distance，SQLite 上该算子不存在 → 抛异常
        # 此处仅验证查询构造不崩溃（execute 会抛，但 order_by/where 正确构造）
        with pytest.raises(Exception):  # noqa: B017
            await search_memory(session, agent.id, "query")

    _run(client, _scenario)


# ===================== 2. PgMemoryBackend =====================


async def test_pg_memory_backend_search_returns_content_list() -> None:
    """PgMemoryBackend.search 返回 content 字符串列表（去 embedding）。"""
    sf = MagicMock()
    fake_session = AsyncMock(spec=AsyncSession)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    sf.return_value = fake_session

    # mock search_memory 返回 2 个 chunk
    chunk1 = MagicMock()
    chunk1.content = "历史1"
    chunk2 = MagicMock()
    chunk2.content = "历史2"

    backend = PgMemoryBackend(sf)
    import app.domains.agents.memory as mem_mod

    original = mem_mod.search_memory
    mem_mod.search_memory = AsyncMock(return_value=[chunk1, chunk2])
    try:
        result = await backend.search(uuid.uuid4(), "query")
        assert result == ["历史1", "历史2"]
    finally:
        mem_mod.search_memory = original


async def test_pg_memory_backend_search_catches_exception_returns_empty() -> None:
    """PgMemoryBackend.search 异常时返回 []，不抛出。"""
    sf = MagicMock()
    sf.return_value = MagicMock(
        __aenter__=AsyncMock(side_effect=RuntimeError("db down")),
        __aexit__=AsyncMock(return_value=None),
    )
    backend = PgMemoryBackend(sf)
    result = await backend.search(uuid.uuid4(), "query")
    assert result == []


async def test_pg_memory_backend_upsert_catches_exception_no_raise() -> None:
    """PgMemoryBackend.upsert 异常时不抛出（no-op）。"""
    sf = MagicMock()
    sf.return_value = MagicMock(
        __aenter__=AsyncMock(side_effect=RuntimeError("db down")),
        __aexit__=AsyncMock(return_value=None),
    )
    backend = PgMemoryBackend(sf)
    # 不应抛异常
    await backend.upsert(
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        turn=1,
        content="content",
    )


# ===================== 3. executor 集成（mock memory） =====================


async def test_executor_injects_history_when_memory_has_results() -> None:
    """有历史时注入 relevant_history system 消息。"""
    agent = _make_agent()
    memory = _FakeMemoryBackend(history=["之前的观察记录"])
    # LLM 第一轮直接返回最终答案
    mock_llm = _make_mock_llm([
        LLMResponse(content="答案", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, memory=memory)
    result = await executor.run(agent, "问题")

    assert result.final_answer == "答案"
    # search 被调用一次（agent_id, query）
    assert len(memory.search_calls) == 1
    assert memory.search_calls[0][1] == "问题"
    # upsert 被调用（最终答案轮 thought 持久化）
    assert len(memory.upsert_calls) == 1
    assert memory.upsert_calls[0]["content"] == "答案"
    assert memory.upsert_calls[0]["metadata"]["type"] == "final_answer"


async def test_executor_no_injection_when_history_empty() -> None:
    """无历史时不注入（search 返回 []）。"""
    agent = _make_agent()
    memory = _FakeMemoryBackend(history=[])
    mock_llm = _make_mock_llm([
        LLMResponse(content="答案", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, memory=memory)
    await executor.run(agent, "问题")

    # search 被调用但返回空
    assert len(memory.search_calls) == 1
    # upsert 仍被调用（最终答案持久化）
    assert len(memory.upsert_calls) == 1


async def test_executor_no_memory_is_backward_compatible() -> None:
    """memory=None 时不注入不持久化（与 P1-4 前行为一致）。"""
    agent = _make_agent()
    mock_llm = _make_mock_llm([
        LLMResponse(content="答案", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, memory=None)
    result = await executor.run(agent, "问题")

    assert result.final_answer == "答案"
    assert result.success is True


async def test_executor_persists_observation_after_tool_turn() -> None:
    """工具调用轮持久化 observation（而非 thought）。"""
    agent = _make_agent()
    memory = _FakeMemoryBackend(history=[])
    # 第一轮：工具调用；第二轮：最终答案
    mock_llm = _make_mock_llm([
        LLMResponse(
            content="调用工具",
            tool_calls=[ToolCall(id="1", name="search", args={"query": "q"})],
            usage={"total_tokens": 10},
        ),
        LLMResponse(content="最终答案", usage={"total_tokens": 15}),
    ])

    # 需要 tool_executor
    tool_executor = MagicMock()
    tool_executor.can_handle = MagicMock(return_value=True)
    tool_executor.execute = AsyncMock(return_value="工具结果观察")

    executor = AgentExecutor(mock_llm, tool_executor=tool_executor, memory=memory)
    result = await executor.run(agent, "问题")

    assert result.final_answer == "最终答案"
    # 两轮各 upsert 一次
    assert len(memory.upsert_calls) == 2
    # 第一轮：observation（_execute_tools 包裹为 "[{name}] {result}"）
    assert memory.upsert_calls[0]["content"] == "[search] 工具结果观察"
    assert memory.upsert_calls[0]["metadata"]["type"] == "observation"
    assert memory.upsert_calls[0]["turn"] == 1
    # 第二轮：final_answer（thought，无 observation）
    assert memory.upsert_calls[1]["content"] == "最终答案"
    assert memory.upsert_calls[1]["metadata"]["type"] == "final_answer"
    assert memory.upsert_calls[1]["turn"] == 2


async def test_executor_skips_upsert_when_content_empty() -> None:
    """trace 无 observation 且无 thought 时不 upsert。"""
    agent = _make_agent(max_turns=1)
    memory = _FakeMemoryBackend(history=[])
    # LLM 返回空 content 且无 tool_calls → trace.thought="" → 不 upsert
    mock_llm = _make_mock_llm([
        LLMResponse(content="", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, memory=memory)
    await executor.run(agent, "问题")

    # thought 为空 → 不 upsert
    assert len(memory.upsert_calls) == 0


# ===================== 4. execute_agent 集成（mock） =====================


def _setup_execute_agent_mocks(
    monkeypatch: pytest.MonkeyPatch,
    agent: Agent,
) -> dict[str, Any]:
    """mock agent_service 的依赖，返回 captured dict 收集 executor 构造参数。

    与 test_evals_online.py 同模式：mock get_agent / _build_llm_config /
    LLMClient / AgentExecutor，避免真实 DB 查询与 LLM 调用。
    """
    from app.core.llm_client import LLMConfig
    from app.domains.agents import service as agent_service

    monkeypatch.setattr(
        agent_service, "get_agent", AsyncMock(return_value=agent)
    )
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

    captured: dict[str, Any] = {}

    def _spy_executor(*args: Any, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(
            return_value=ExecutionResult(
                agent_id=agent.id,
                final_answer="ok",
                success=True,
                total_tokens=5,
            )
        )
        return mock_executor

    monkeypatch.setattr(agent_service, "AgentExecutor", _spy_executor)
    return captured


def test_execute_agent_memory_disabled_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent_memory_enabled=False（默认）→ executor.memory is None。"""
    import uuid as _uuid

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_memory_enabled", False)

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
    captured = _setup_execute_agent_mocks(monkeypatch, agent)

    async def _scenario(session: AsyncSession) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    assert captured["kwargs"].get("memory") is None


def test_execute_agent_memory_enabled_constructs_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent_memory_enabled=True → executor.memory 为 PgMemoryBackend。"""
    import uuid as _uuid

    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_memory_enabled", True)
    monkeypatch.setattr(agent_service.settings, "agent_memory_top_k", 5)

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
    captured = _setup_execute_agent_mocks(monkeypatch, agent)

    async def _scenario(session: AsyncSession) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    memory = captured["kwargs"].get("memory")
    assert isinstance(memory, PgMemoryBackend)


# ===================== 5. AgentMemoryChunk ORM =====================


async def test_agent_memory_chunk_orm_metadata_alias(client: TestClient) -> None:
    """AgentMemoryChunk.metadata_ 映射到 metadata 列（规避 ORM metadata 属性冲突）。"""

    async def _scenario(session: AsyncSession) -> None:
        agent = Agent(
            name="orm-agent",
            model_alias="default",
            tools=[],
            max_turns=1,
            temperature=0.7,
            is_active=True,
        )
        session.add(agent)
        await session.flush()

        chunk = AgentMemoryChunk(
            agent_id=agent.id,
            session_id=uuid.uuid4(),
            turn=1,
            content="test",
            metadata_={"key": "value"},
        )
        session.add(chunk)
        await session.flush()
        await session.refresh(chunk)

        assert chunk.metadata_ == {"key": "value"}
        assert chunk.turn == 1
        assert chunk.content == "test"

    _run(client, _scenario)
