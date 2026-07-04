"""查询改写 / HyDE（P1-5）测试 — LLM 改写 + 多 query 并发检索去重。

覆盖三层：
1. **QueryRewriter**：
   - 正常生成变体 + HyDE（mock LLM 返回 JSON 数组 + 文本）
   - LLM 失败降级为原始 query
   - JSON 解析宽容（```json 包裹、多余文本、无 JSON）
   - 去重（变体与原始重复）
   - n_variants=0 + hyde=False → 仅原始 query
2. **MultiQueryMemoryBackend**：
   - 多 query 并发检索 + 按频次去重
   - 单 query 退化为底层 backend
   - rewriter 失败降级
   - 子检索异常跳过
   - upsert 转发
3. **execute_agent 集成**（mock）：
   - memory 开 + rewrite 开 → MultiQueryMemoryBackend
   - memory 开 + rewrite 关 → PgMemoryBackend
   - memory 关 → None
4. **_parse_variants 纯函数**：边界用例
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.llm_client import LLMClient, LLMResponse, Message
from app.domains.agents.memory import PgMemoryBackend
from app.domains.agents.models import Agent, ExecutionResult
from app.domains.agents.query_rewrite import (
    MultiQueryMemoryBackend,
    QueryRewriter,
    _parse_variants,
)
from app.main import app

# ===================== 辅助 =====================


def _make_llm(responses: list[LLMResponse]) -> Any:
    """按顺序返回不同响应的 mock LLMClient。"""
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


class _FakeBackend:
    """记录 search 调用的假记忆后端。"""

    def __init__(self, results_map: dict[str, list[str]] | None = None) -> None:
        self._map = results_map or {}
        self.search_calls: list[tuple[uuid.UUID, str, int]] = []
        self.upsert_calls: list[dict[str, Any]] = []

    async def search(
        self, agent_id: uuid.UUID, query: str, top_k: int = 3
    ) -> list[str]:
        self.search_calls.append((agent_id, query, top_k))
        return list(self._map.get(query, []))

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
            {"agent_id": agent_id, "turn": turn, "content": content}
        )


def _run(client: TestClient, scenario: Any) -> None:
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


# ===================== 1. QueryRewriter =====================


async def test_rewriter_generates_variants_and_hyde() -> None:
    """正常生成变体 + HyDE，返回原始 + 变体 + HyDE（去重）。"""
    # 变体响应在前，HyDE 响应在后
    llm = _make_llm([
        LLMResponse(content='["如何部署应用", "应用部署步骤"]'),  # 变体
        LLMResponse(content="假设答案：使用 docker compose up 部署"),  # HyDE
    ])
    rw = QueryRewriter(llm, n_variants=2, enable_hyde=True)
    result = await rw.rewrite("怎么部署")
    assert result[0] == "怎么部署"  # 原始首位
    assert "如何部署应用" in result
    assert "应用部署步骤" in result
    assert "假设答案：使用 docker compose up 部署" in result
    assert len(result) == 4


async def test_rewriter_fallback_on_llm_failure() -> None:
    """LLM 异常时降级为仅原始 query。"""
    llm = MagicMock(spec=LLMClient)

    async def _boom(_: Any) -> LLMResponse:
        raise RuntimeError("llm down")

    llm.chat = _boom
    rw = QueryRewriter(llm, n_variants=2, enable_hyde=True)
    result = await rw.rewrite("原始问题")
    assert result == ["原始问题"]


async def test_rewriter_dedup_against_original() -> None:
    """变体与原始重复时去重。"""
    llm = _make_llm([
        LLMResponse(content='["原始问题", "另一变体"]'),  # 变体含原始
        LLMResponse(content="假设答案"),
    ])
    rw = QueryRewriter(llm, n_variants=2, enable_hyde=True)
    result = await rw.rewrite("原始问题")
    # 原始只出现一次
    assert result.count("原始问题") == 1
    assert "另一变体" in result


async def test_rewriter_no_variants_no_hyde_returns_original_only() -> None:
    """n_variants=0 + hyde=False → 仅原始 query，不调 LLM。"""
    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(side_effect=AssertionError("不应调用 LLM"))
    rw = QueryRewriter(llm, n_variants=0, enable_hyde=False)
    result = await rw.rewrite("q")
    assert result == ["q"]


async def test_rewriter_hyde_empty_string_skipped() -> None:
    """HyDE 返回空串时跳过。"""
    llm = _make_llm([
        LLMResponse(content='["变体1"]'),
        LLMResponse(content="   "),  # HyDE 空白
    ])
    rw = QueryRewriter(llm, n_variants=1, enable_hyde=True)
    result = await rw.rewrite("q")
    assert result == ["q", "变体1"]


# ===================== 2. _parse_variants 纯函数 =====================


def test_parse_variants_plain_json() -> None:
    resp = LLMResponse(content='["a", "b"]')
    assert _parse_variants(resp, 2) == ["a", "b"]


def test_parse_variants_json_code_fence() -> None:
    resp = LLMResponse(content='```json\n["a", "b"]\n```')
    assert _parse_variants(resp, 2) == ["a", "b"]


def test_parse_variants_with_extra_text() -> None:
    resp = LLMResponse(content='这是改写结果：\n["a", "b"]\n希望有帮助')
    assert _parse_variants(resp, 2) == ["a", "b"]


def test_parse_variants_no_json_returns_empty() -> None:
    resp = LLMResponse(content="无法生成改写")
    assert _parse_variants(resp, 2) == []


def test_parse_variants_truncates_to_n_expected() -> None:
    resp = LLMResponse(content='["a", "b", "c", "d"]')
    assert _parse_variants(resp, 2) == ["a", "b"]


def test_parse_variants_non_list_returns_empty() -> None:
    resp = LLMResponse(content='{"key": "value"}')
    assert _parse_variants(resp, 2) == []


# ===================== 3. MultiQueryMemoryBackend =====================


async def test_multi_query_concurrent_search_dedup_by_frequency() -> None:
    """多 query 并发检索，按频次去重排序。"""
    # 三个 query 各自召回不同的 content，其中 "共同文档" 被 2 个 query 召回
    backend = _FakeBackend({
        "q1": ["共同文档", "doc1"],
        "q2": ["共同文档", "doc2"],
        "q3": ["doc3"],
    })
    rw = MagicMock(spec=QueryRewriter)
    rw.rewrite = AsyncMock(return_value=["q1", "q2", "q3"])
    multi = MultiQueryMemoryBackend(backend, rw)  # type: ignore[arg-type]

    result = await multi.search(uuid.uuid4(), "原始", top_k=3)
    # "共同文档" 频次 2 排首位
    assert result[0] == "共同文档"
    # 其余频次 1，保持首次出现顺序
    assert "doc1" in result
    assert "doc2" in result
    assert len(result) == 3  # top_k=3
    # 底层 search 被调用 3 次（每个 query 一次）
    assert len(backend.search_calls) == 3


async def test_multi_query_single_query_fallback_to_backend() -> None:
    """rewriter 只返回 1 个 query 时退化为底层 backend 单 query。"""
    backend = _FakeBackend({"q": ["doc"]})
    rw = MagicMock(spec=QueryRewriter)
    rw.rewrite = AsyncMock(return_value=["q"])
    multi = MultiQueryMemoryBackend(backend, rw)  # type: ignore[arg-type]

    result = await multi.search(uuid.uuid4(), "q", top_k=3)
    assert result == ["doc"]
    assert len(backend.search_calls) == 1


async def test_multi_query_subsearch_failure_skipped() -> None:
    """子检索异常时跳过该路，不影响其他路。"""

    class _FlakyBackend(_FakeBackend):
        async def search(
            self, agent_id: uuid.UUID, query: str, top_k: int = 3
        ) -> list[str]:
            if query == "q2":
                raise RuntimeError("db error")
            return await super().search(agent_id, query, top_k)

    backend = _FlakyBackend({"q1": ["doc1"], "q3": []})
    rw = MagicMock(spec=QueryRewriter)
    rw.rewrite = AsyncMock(return_value=["q1", "q2", "q3"])
    multi = MultiQueryMemoryBackend(backend, rw)  # type: ignore[arg-type]

    result = await multi.search(uuid.uuid4(), "x", top_k=3)
    # q1 召回 doc1，q2 异常跳过，q3 无结果
    assert "doc1" in result


async def test_multi_query_rewrite_failure_fallback_single() -> None:
    """rewriter.rewrite 异常时降级为底层单 query 检索。"""
    backend = _FakeBackend({"q": ["doc"]})
    rw = MagicMock(spec=QueryRewriter)
    rw.rewrite = AsyncMock(side_effect=RuntimeError("llm down"))
    multi = MultiQueryMemoryBackend(backend, rw)  # type: ignore[arg-type]

    result = await multi.search(uuid.uuid4(), "q", top_k=3)
    assert result == ["doc"]


async def test_multi_query_upsert_forwards_to_backend() -> None:
    """upsert 直接转发到底层 backend。"""
    backend = _FakeBackend()
    rw = MagicMock(spec=QueryRewriter)
    multi = MultiQueryMemoryBackend(backend, rw)  # type: ignore[arg-type]

    await multi.upsert(
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        turn=1,
        content="observation",
    )
    assert len(backend.upsert_calls) == 1
    assert backend.upsert_calls[0]["content"] == "observation"


# ===================== 4. execute_agent 集成（mock） =====================


def _setup_execute_agent_mocks(
    monkeypatch: pytest.MonkeyPatch, agent: Agent
) -> dict[str, Any]:
    from app.core.llm_client import LLMConfig
    from app.domains.agents import service as agent_service

    monkeypatch.setattr(agent_service, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(
        agent_service,
        "_build_llm_config",
        AsyncMock(return_value=LLMConfig(provider="openai", model="m", api_key="k")),
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


def test_execute_agent_memory_and_rewrite_both_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory 开 + rewrite 开 → MultiQueryMemoryBackend。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_memory_enabled", True)
    monkeypatch.setattr(agent_service.settings, "agent_query_rewrite_enabled", True)

    agent = Agent(
        id=uuid.uuid4(),
        name="t",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )
    captured = _setup_execute_agent_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    assert isinstance(captured["kwargs"].get("memory"), MultiQueryMemoryBackend)


def test_execute_agent_memory_on_rewrite_off_returns_pg_backend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory 开 + rewrite 关 → PgMemoryBackend。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_memory_enabled", True)
    monkeypatch.setattr(agent_service.settings, "agent_query_rewrite_enabled", False)

    agent = Agent(
        id=uuid.uuid4(),
        name="t",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )
    captured = _setup_execute_agent_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    assert isinstance(captured["kwargs"].get("memory"), PgMemoryBackend)


def test_execute_agent_memory_off_returns_none_even_if_rewrite_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory 关 → None（rewrite 依赖 memory，无 memory 则 rewrite 无意义）。"""
    from app.domains.agents import service as agent_service
    from app.domains.agents.models import ExecuteRequest

    monkeypatch.setattr(agent_service.settings, "agent_memory_enabled", False)
    monkeypatch.setattr(agent_service.settings, "agent_query_rewrite_enabled", True)

    agent = Agent(
        id=uuid.uuid4(),
        name="t",
        system_prompt="x",
        model_alias="default",
        tools=[],
        max_turns=1,
        temperature=0.7,
        is_active=True,
    )
    captured = _setup_execute_agent_mocks(monkeypatch, agent)

    async def _scenario(session: Any) -> None:
        await agent_service.execute_agent(
            session, agent.id, ExecuteRequest(input="test")
        )

    _run(client, _scenario)
    assert captured["kwargs"].get("memory") is None
