"""Agent Orchestrator — 单元测试。

覆盖 service 纯函数 + executor 解析逻辑。
LLM 调用使用 stub，不依赖真实 API。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.llm_client import LLMClient, LLMResponse
from app.domains.agents import service
from app.domains.agents.executor import AgentExecutor, _build_tool_prompt
from app.domains.agents.models import (
    AgentCreate,
    ToolDef,
    ToolType,
)


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
async def test_create_agent(session: AsyncSession) -> None:
    agent = await service.create_agent(
        session,
        AgentCreate(
            name="researcher",
            description="研究助手",
            system_prompt="你是一名研究员",
            tools=[ToolDef(name="search", type=ToolType.SEARCH)],
        ),
    )
    assert agent.name == "researcher"
    assert agent.max_turns == 10
    assert agent.tools[0]["name"] == "search"


@pytest.mark.asyncio
async def test_list_and_get_agent(session: AsyncSession) -> None:
    created = await service.create_agent(session, AgentCreate(name="a1", max_turns=5))
    agents = await service.list_agents(session)
    assert len(agents) == 1
    fetched = await service.get_agent(session, created.id)
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_execute_agent_without_tools(session: AsyncSession) -> None:
    """stub LLM 直接返回 final answer（无 tool_calls 块）。"""
    agent = await service.create_agent(
        session,
        AgentCreate(name="qa", system_prompt="回答问题", max_turns=3),
    )

    stub_client = MagicMock(spec=LLMClient)
    stub_client.chat = AsyncMock(
        return_value=LLMResponse(content="答案是 42", usage={"total_tokens": 10})
    )
    stub_client.close = AsyncMock()
    executor = AgentExecutor(stub_client)
    result = await executor.run(agent, "终极问题是什么？")
    assert result.final_answer == "答案是 42"
    assert result.total_tokens == 10
    assert len(result.traces) == 1


@pytest.mark.asyncio
async def test_executor_parses_tool_calls(session: AsyncSession) -> None:
    """LLM 输出含 tool_calls 块，应触发工具调用循环。"""
    agent = await service.create_agent(
        session,
        AgentCreate(
            name="tool-user",
            system_prompt="用工具",
            tools=[ToolDef(name="calc", type=ToolType.CALCULATOR)],
            max_turns=3,
        ),
    )

    class StubTool:
        async def execute(self, tool: ToolDef, args: dict[str, Any]) -> str:
            return f"计算结果: {args}"

        def can_handle(self, tool_type: ToolType) -> bool:
            return True

    call_count = {"n": 0}

    async def fake_chat(messages: Any) -> LLMResponse:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="```tool_calls\n[{\"name\": \"calc\", \"args\": {\"expr\": \"1+1\"}}]\n```",
                usage={"total_tokens": 5},
            )
        return LLMResponse(content="最终结果 2", usage={"total_tokens": 5})

    stub_client = MagicMock(spec=LLMClient)
    stub_client.chat = fake_chat
    stub_client.close = AsyncMock()
    executor = AgentExecutor(stub_client, tool_executor=StubTool())
    result = await executor.run(agent, "算 1+1")
    assert "2" in result.final_answer or "最终结果" in result.final_answer
    assert len(result.traces) >= 2


def test_build_tool_prompt_empty() -> None:
    assert _build_tool_prompt([]) == ""


def test_build_tool_prompt_lists_tools() -> None:
    prompt = _build_tool_prompt([ToolDef(name="search", type=ToolType.SEARCH, description="搜索")])
    assert "search" in prompt
    assert "tool_calls" in prompt
