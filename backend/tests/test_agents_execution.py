"""Agent Orchestrator 执行追踪与工作流 DAG eval（agents/SPEC.md Success Criteria）。

P0-2 重构后覆盖：
1. ReAct 循环在无工具调用时正确终止并返回最终答案
2. 达到 max_turns 仍无最终答案时 success=False（P2-8：保留最后输出）
3. 单工具执行异常被隔离捕获，不中断整体循环（P2-8 并发 + return_exceptions）
4. DAG 节点 > 50 时创建与执行均报错
5. 每轮 ExecutionTrace 完整记录 thought/action/observation/tokens
6. Agent.tools 转为 LLMClient 原生 ToolDef（P0-2 _agent_tools_to_llm_tools）

直接测 executor 层（AgentExecutor.run / execute_workflow_dag /
_agent_tools_to_llm_tools），通过注入 mock llm_client.chat 控制每轮 LLM
输出，避免真实网络调用。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ValidationError
from app.core.llm_client import LLMResponse, ToolCall
from app.domains.agents import service as agent_service
from app.domains.agents.executor import (
    AgentExecutor,
    _agent_tools_to_llm_tools,
    execute_workflow_dag,
)
from app.domains.agents.models import (
    Agent,
    ToolDef,
    ToolType,
)

# ===================== 辅助：构造 Agent 与 Mock LLM =====================


def _agent(
    *,
    tools: list[ToolDef] | None = None,
    max_turns: int = 5,
    system_prompt: str = "you are a researcher",
) -> Agent:
    """构造内存 Agent（不入库）。"""
    return Agent(
        id=uuid.uuid4(),
        name="test-agent",
        description="test",
        system_prompt=system_prompt,
        model_alias="default",
        tools=[t.model_dump() for t in (tools or [])],
        max_turns=max_turns,
        temperature=0.7,
        is_active=True,
    )


def _tool(name: str = "search", type_: ToolType = ToolType.SEARCH) -> ToolDef:
    return ToolDef(name=name, type=type_, description=f"{name} tool")


def _llm_response(
    content: str = "",
    total_tokens: int = 100,
    tool_calls: list[ToolCall] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content, tool_calls=tool_calls or [], usage={"total_tokens": total_tokens}
    )


def _executor_with_chat(
    chat_side_effect: Any, tool_executor: Any = None
) -> AgentExecutor:
    """构造 AgentExecutor，注入 mock chat。

    P0-2：chat 签名改为 (messages, tools=None, response_format=None)，
    AsyncMock side_effect 需接受可选参数。用包装函数兼容。
    """
    llm = AsyncMock()

    async def _chat(
        messages: list[Any],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if isinstance(chat_side_effect, list):
            idx = _chat._idx  # type: ignore[attr-defined]
            _chat._idx = min(idx + 1, len(chat_side_effect) - 1)  # type: ignore[attr-defined]
            return chat_side_effect[idx]
        return await chat_side_effect(messages, tools=tools, response_format=response_format)

    _chat._idx = 0  # type: ignore[attr-defined]
    _chat.await_count = 0  # type: ignore[attr-defined]

    async def _counting_chat(*args: Any, **kwargs: Any) -> LLMResponse:
        _counting_chat.await_count += 1  # type: ignore[attr-defined]
        return await _chat(*args, **kwargs)

    _counting_chat.await_count = 0  # type: ignore[attr-defined]
    llm.chat = _counting_chat
    return AgentExecutor(llm, tool_executor=tool_executor)


# ===================== 1. ReAct 无工具调用 → 终止 =====================


@pytest.mark.asyncio
async def test_react_terminates_when_no_tool_calls() -> None:
    """ReAct 循环在 LLM 无工具调用时立即终止，返回最终答案（SPEC 1）。"""
    agent = _agent(max_turns=5)
    executor = _executor_with_chat(
        [_llm_response("最终答案是 42", total_tokens=42)]
    )

    result = await executor.run(agent, "what is the answer?")

    assert result.success is True
    assert result.final_answer == "最终答案是 42"
    assert len(result.traces) == 1
    assert executor.llm.chat.await_count == 1  # type: ignore[attr-defined]


# ===================== 2. max_turns 截断 → success=False =====================


@pytest.mark.asyncio
async def test_max_turns_truncation_returns_failure() -> None:
    """达到 max_turns 仍无最终答案 → success=False（SPEC 2 + P2-8：保留最后输出）。"""
    agent = _agent(max_turns=2)
    # 每轮都返回原生 tool_calls，永远不给出最终答案
    tool_call = ToolCall(id="t1", name="search", args={"query": "weather"})
    executor = _executor_with_chat(
        [_llm_response("", 50, tool_calls=[tool_call])] * 3
    )

    result = await executor.run(agent, "查询天气")

    assert result.success is False  # 截断视为失败
    # P2-8：保留最后输出（assistant content），而非固定失败消息
    assert executor.llm.chat.await_count == 2  # type: ignore[attr-defined]
    assert len(result.traces) == 2


# ===================== 3. 单工具异常隔离 =====================


@pytest.mark.asyncio
async def test_tool_exception_isolated() -> None:
    """单工具执行异常被隔离捕获，记录到 observation 但不中断循环（SPEC 3 + P2-8 并发）。"""
    tool = _tool("search")

    class _FlakyToolExecutor:
        def can_handle(self, tool_type: ToolType) -> bool:
            return True

        async def execute(self, t: Any, args: dict[str, Any]) -> str:
            raise RuntimeError("工具爆炸了")

    agent = _agent(tools=[tool], max_turns=3)
    # 第 1 轮：调用工具 → 异常被隔离；第 2 轮：给出最终答案
    tool_call = ToolCall(id="t1", name="search", args={"query": "weather"})
    executor = _executor_with_chat(
        [
            _llm_response("", 50, tool_calls=[tool_call]),
            _llm_response("最终答案：无法查询", 30),
        ],
        tool_executor=_FlakyToolExecutor(),
    )

    result = await executor.run(agent, "查询天气")

    assert result.success is True
    assert result.final_answer == "最终答案：无法查询"
    # 第 1 轮 observation 应记录异常信息（不抛错）
    assert len(result.traces) == 2
    assert "search 错误" in (result.traces[0].observation or "")
    assert "工具爆炸了" in (result.traces[0].observation or "")


# ===================== 4. DAG 节点 > 50 报错 =====================


@pytest.mark.asyncio
async def test_dag_create_workflow_rejects_over_50_nodes() -> None:
    """create_workflow 在节点 > 50 时抛 ValidationError（SPEC 4 - 创建路径）。"""
    from app.domains.agents.models import (
        AgentNode,
        WorkflowDef,
    )

    nodes = [
        AgentNode(id=f"n{i}", name=f"node-{i}") for i in range(51)
    ]
    payload = WorkflowDef(name="too-big", nodes=nodes)

    class _NoopSession:
        async def add(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def flush(self) -> None:
            pass

    with pytest.raises(ValidationError) as exc_info:
        await agent_service.create_workflow(_NoopSession(), payload)  # type: ignore[arg-type]
    assert "50" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dag_execute_rejects_over_50_nodes() -> None:
    """execute_workflow_dag 节点 > 50 时抛 ValidationError（SPEC 4：输入校验，非 LLM 失败）。"""
    nodes = [{"id": f"n{i}", "name": f"node-{i}"} for i in range(51)]

    with pytest.raises(ValidationError) as exc_info:
        await execute_workflow_dag(
            uuid.uuid4(), nodes, [], AsyncMock(), "input"
        )
    assert "50" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dag_empty_nodes_raises() -> None:
    """DAG 节点为空时抛 ValidationError（SPEC Error Cases 补充验证）。"""
    with pytest.raises(ValidationError) as exc_info:
        await execute_workflow_dag(uuid.uuid4(), [], [], AsyncMock(), "x")
    assert "无节点" in str(exc_info.value)


# ===================== 5. ExecutionTrace 完整记录 =====================


@pytest.mark.asyncio
async def test_execution_trace_records_all_fields() -> None:
    """每轮 ExecutionTrace 完整记录 thought/action/observation/tokens（SPEC 5）。"""
    tool = _tool("calc")

    class _RecordingToolExecutor:
        def can_handle(self, tool_type: ToolType) -> bool:
            return True

        async def execute(self, t: Any, args: dict[str, Any]) -> str:
            return "结果=42"

    agent = _agent(tools=[tool], max_turns=3)
    tool_call = ToolCall(id="t1", name="calc", args={"expr": "6*7"})
    executor = _executor_with_chat(
        [
            _llm_response("", 50, tool_calls=[tool_call]),
            _llm_response("答案是 42", 30),
        ],
        tool_executor=_RecordingToolExecutor(),
    )

    result = await executor.run(agent, "6*7=?")

    assert len(result.traces) == 2

    # 第 1 轮：有工具调用，应记录 action / observation / thought / tokens
    t1 = result.traces[0]
    assert t1.turn == 1
    assert t1.action is not None and "calc" in t1.action  # tool_calls JSON
    assert t1.observation is not None and "结果=42" in t1.observation
    assert t1.tokens == 50  # 累计 tokens

    # 第 2 轮：无工具调用（终止轮），action/observation 为 None
    t2 = result.traces[1]
    assert t2.turn == 2
    assert t2.thought == "答案是 42"
    assert t2.action is None  # 终止轮无 action
    assert t2.observation is None
    # tokens 累计：prev(50) + current(30) = 80
    assert t2.tokens == 80
    assert result.total_tokens == 80


# ===================== 6. _agent_tools_to_llm_tools（P0-2） =====================


def test_agent_tools_to_llm_tools_injects_tool_descriptions() -> None:
    """Agent.tools 转为 LLMClient 原生 ToolDef（P0-2 替代 _build_tool_prompt）。"""
    tools = [
        {"name": "search", "type": "search", "description": "web 搜索"},
        {"name": "calc", "type": "calculator", "description": "计算器"},
    ]
    llm_tools = _agent_tools_to_llm_tools(tools)

    assert len(llm_tools) == 2
    assert llm_tools[0].name == "search"
    assert llm_tools[0].description == "web 搜索"
    assert "query" in llm_tools[0].parameters["properties"]
    assert llm_tools[1].name == "calc"
    assert llm_tools[1].description == "计算器"
    assert "expr" in llm_tools[1].parameters["properties"]


def test_agent_tools_to_llm_tools_empty_tools_returns_empty() -> None:
    """无工具时返回空列表。"""
    assert _agent_tools_to_llm_tools([]) == []


@pytest.mark.asyncio
async def test_tools_passed_to_llm_chat() -> None:
    """Agent.tools 转为 ToolDef 后通过 chat(tools=...) 传入 LLM（P0-2 端到端验证）。"""
    tools = [_tool("search")]
    agent = _agent(tools=tools, max_turns=1)
    captured_tools: list[Any] = []

    async def _capture_chat(
        messages: list[Any], tools: list[Any] | None = None, **kw: Any
    ) -> LLMResponse:
        captured_tools.extend(tools or [])
        return _llm_response("done", 10)

    executor = _executor_with_chat(_capture_chat)
    await executor.run(agent, "hi")

    # P0-2：tools 应作为原生 ToolDef 传入 chat
    assert len(captured_tools) == 1
    assert captured_tools[0].name == "search"
