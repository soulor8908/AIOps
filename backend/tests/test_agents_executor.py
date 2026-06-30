"""agents/executor.py 单元测试 — ReAct 循环。

使用 mock LLMClient，不依赖真实 LLM API。

覆盖：
- 无工具时直接返回答案
- 有工具调用时执行工具
- 达到最大轮次
- 工具不存在时的处理
- 工具抛异常时的处理
- parse_tool_calls_json 解析（有效/无效）
- execute_workflow_dag DAG 执行
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import LLMError
from app.core.llm_client import LLMClient, LLMResponse, Message, parse_tool_calls_json
from app.domains.agents.executor import (
    AgentExecutor,
    _build_tool_prompt,
    execute_workflow_dag,
)
from app.domains.agents.models import (
    Agent,
    ExecutionResult,
    ToolDef,
    ToolType,
)


# ===================== 辅助函数 =====================

def _make_agent(
    name: str = "test-agent",
    tools: list[dict[str, Any]] | None = None,
    max_turns: int = 3,
    system_prompt: str | None = None,
) -> Agent:
    """构造无需 DB 的 Agent 实例。"""
    return Agent(
        id=uuid.uuid4(),
        name=name,
        system_prompt=system_prompt,
        tools=tools or [],
        max_turns=max_turns,
    )


def _make_mock_llm(responses: list[LLMResponse]) -> Any:
    """构造按顺序返回不同响应的 mock LLMClient。"""
    call_idx = {"n": 0}

    async def fake_chat(messages: list[Message]) -> LLMResponse:
        idx = min(call_idx["n"], len(responses) - 1)
        call_idx["n"] += 1
        return responses[idx]

    stub = MagicMock(spec=LLMClient)
    stub.chat = fake_chat
    stub.close = AsyncMock()
    return stub


# ===================== 无工具 — 直接返回 =====================

async def test_executor_no_tools_returns_answer() -> None:
    """无工具时直接返回。"""
    agent = _make_agent(system_prompt="回答问题", max_turns=3)
    mock_llm = _make_mock_llm([
        LLMResponse(content="答案是 42", usage={"total_tokens": 10}),
    ])

    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "终极问题是什么？")

    assert result.final_answer == "答案是 42"
    assert result.success is True
    assert result.total_tokens == 10
    assert len(result.traces) == 1
    assert result.traces[0].thought == "答案是 42"
    assert result.traces[0].action is None  # 无工具调用


async def test_executor_no_tools_with_context() -> None:
    """带 context 参数时消息包含 context。"""
    agent = _make_agent(system_prompt="助手", max_turns=3)
    mock_llm = _make_mock_llm([
        LLMResponse(content="ok", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm)
    result = await executor.run(
        agent, "hello", context={"key": "value"}
    )

    assert result.final_answer == "ok"


# ===================== 有工具调用 =====================

class _StubToolExecutor:
    """简单的工具执行器 stub。"""

    def __init__(self, result_map: dict[str, str] | None = None) -> None:
        self.result_map = result_map or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def can_handle(self, tool_type: ToolType) -> bool:
        return True

    async def execute(self, tool: ToolDef, args: dict[str, Any]) -> str:
        self.calls.append((tool.name, args))
        return self.result_map.get(tool.name, f"result for {tool.name}")


async def test_executor_with_tool_call() -> None:
    """有工具调用时执行工具。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator", "description": "计算器"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        # 第一轮：返回工具调用
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {"expr": "1+1"}}]\n```',
            usage={"total_tokens": 5},
        ),
        # 第二轮：返回最终答案
        LLMResponse(content="最终结果 2", usage={"total_tokens": 5}),
    ])

    tool_exec = _StubToolExecutor({"calc": "计算结果: 2"})
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "算 1+1")

    assert "2" in result.final_answer or "最终结果" in result.final_answer
    assert len(result.traces) == 2
    # 第一轮有工具调用
    assert result.traces[0].action is not None
    assert "calc" in result.traces[0].action
    assert "计算结果: 2" in result.traces[0].observation
    # 第二轮是最终答案
    assert result.traces[1].action is None
    # 工具被调用
    assert len(tool_exec.calls) == 1
    assert tool_exec.calls[0][0] == "calc"
    assert tool_exec.calls[0][1] == {"expr": "1+1"}


async def test_executor_multiple_tool_calls_in_one_turn() -> None:
    """单轮多个工具调用。"""
    agent = _make_agent(
        tools=[
            {"name": "search", "type": "search"},
            {"name": "calc", "type": "calculator"},
        ],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content=(
                '```tool_calls\n'
                '[{"name": "search", "args": {"q": "test"}}, '
                '{"name": "calc", "args": {"expr": "2+2"}}]\n```'
            ),
            usage={"total_tokens": 8},
        ),
        LLMResponse(content="done", usage={"total_tokens": 2}),
    ])

    tool_exec = _StubToolExecutor({
        "search": "found: hello",
        "calc": "result: 4",
    })
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "multi")

    assert result.final_answer == "done"
    assert len(tool_exec.calls) == 2
    assert tool_exec.calls[0][0] == "search"
    assert tool_exec.calls[1][0] == "calc"
    # 观察结果包含两个工具的输出
    assert "found: hello" in result.traces[0].observation
    assert "result: 4" in result.traces[0].observation


# ===================== 最大轮次 =====================

async def test_executor_max_turns_reached() -> None:
    """达到最大轮次。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=2,
    )
    # LLM 始终返回工具调用，不给出最终答案
    mock_llm = _make_mock_llm([
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {}}]\n```',
            usage={"total_tokens": 5},
        ),
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {}}]\n```',
            usage={"total_tokens": 5},
        ),
    ])

    tool_exec = _StubToolExecutor({"calc": "result"})
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "loop")

    assert result.final_answer == "达到最大轮次仍未给出最终答案。"
    assert len(result.traces) == 2  # 每轮一个 trace
    assert result.traces[0].turn == 1
    assert result.traces[1].turn == 2


async def test_executor_max_turns_override() -> None:
    """max_turns 参数覆盖 agent.max_turns（取较小值）。"""
    agent = _make_agent(max_turns=10)
    mock_llm = _make_mock_llm([
        LLMResponse(content="immediate answer", usage={"total_tokens": 1}),
    ])

    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "hi", max_turns=1)

    assert result.final_answer == "immediate answer"
    assert len(result.traces) == 1


# ===================== 工具不存在 =====================

async def test_executor_tool_not_found() -> None:
    """工具不存在时的处理。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        # 调用不存在的工具
        LLMResponse(
            content='```tool_calls\n[{"name": "unknown_tool", "args": {}}]\n```',
            usage={"total_tokens": 5},
        ),
        LLMResponse(content="最终答案", usage={"total_tokens": 3}),
    ])

    tool_exec = _StubToolExecutor()
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "test")

    assert result.final_answer == "最终答案"
    # 观察结果应包含未知工具提示
    assert "未知工具" in result.traces[0].observation
    assert "unknown_tool" in result.traces[0].observation
    # 工具执行器未被调用
    assert len(tool_exec.calls) == 0


# ===================== 工具异常 =====================

class _ErrorToolExecutor:
    """总是抛异常的工具执行器。"""

    def can_handle(self, tool_type: ToolType) -> bool:
        return True

    async def execute(self, tool: ToolDef, args: dict[str, Any]) -> str:
        raise RuntimeError("tool crashed")


async def test_executor_tool_exception() -> None:
    """工具抛异常时的处理。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {"x": 1}}]\n```',
            usage={"total_tokens": 5},
        ),
        LLMResponse(content="恢复后的答案", usage={"total_tokens": 3}),
    ])

    executor = AgentExecutor(mock_llm, tool_executor=_ErrorToolExecutor())
    result = await executor.run(agent, "test")

    assert result.final_answer == "恢复后的答案"
    # 观察结果应包含错误信息
    assert "错误" in result.traces[0].observation
    assert "tool crashed" in result.traces[0].observation
    assert "calc" in result.traces[0].observation


# ===================== 无工具执行器 =====================

async def test_executor_no_tool_executor_configured() -> None:
    """有工具调用但未配置 tool_executor 时，返回跳过提示。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {}}]\n```',
            usage={"total_tokens": 5},
        ),
        LLMResponse(content="answer", usage={"total_tokens": 2}),
    ])

    executor = AgentExecutor(mock_llm)  # 不传 tool_executor
    result = await executor.run(agent, "test")

    assert result.final_answer == "answer"
    assert "未配置" in result.traces[0].observation


# ===================== token 累积 =====================

async def test_executor_accumulates_tokens() -> None:
    """多轮调用累积 token 计数。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {}}]\n```',
            usage={"total_tokens": 10},
        ),
        LLMResponse(
            content='```tool_calls\n[{"name": "calc", "args": {}}]\n```',
            usage={"total_tokens": 20},
        ),
        LLMResponse(content="done", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, tool_executor=_StubToolExecutor())
    result = await executor.run(agent, "test")

    # total_tokens = 10 + 20 + 5 = 35
    assert result.total_tokens == 35


# ===================== parse_tool_calls_json =====================

def test_parse_tool_calls_valid() -> None:
    """解析有效的工具调用格式。"""
    content = (
        'thinking...\n'
        '```tool_calls\n'
        '[{"name": "search", "args": {"q": "hello"}}]\n'
        '```\n'
        'done'
    )
    result = parse_tool_calls_json(content)
    assert len(result) == 1
    assert result[0]["name"] == "search"
    assert result[0]["args"] == {"q": "hello"}


def test_parse_tool_calls_invalid() -> None:
    """无效格式的处理。"""
    # 无标记
    assert parse_tool_calls_json("no tool calls here") == []
    # 空
    assert parse_tool_calls_json("") == []
    # 无效 JSON
    assert parse_tool_calls_json("```tool_calls\n{bad json}\n```") == []
    # 标记但无内容
    result = parse_tool_calls_json("```tool_calls\n```")
    assert result == []


def test_parse_tool_calls_single_object_not_array() -> None:
    """单个 JSON 对象也能解析为列表。"""
    content = '```tool_calls\n{"name": "x", "args": {}}\n```'
    result = parse_tool_calls_json(content)
    assert len(result) == 1
    assert result[0]["name"] == "x"


# ===================== _build_tool_prompt =====================

def test_build_tool_prompt_empty() -> None:
    """空工具列表返回空字符串。"""
    assert _build_tool_prompt([]) == ""


def test_build_tool_prompt_with_tools() -> None:
    """有工具时生成提示。"""
    prompt = _build_tool_prompt([
        ToolDef(name="search", type=ToolType.SEARCH, description="搜索"),
        ToolDef(name="calc", type=ToolType.CALCULATOR, description="计算"),
    ])
    assert "search" in prompt
    assert "calc" in prompt
    assert "tool_calls" in prompt
    assert "搜索" in prompt
    assert "计算" in prompt


# ===================== execute_workflow_dag =====================

async def test_execute_workflow_dag_basic() -> None:
    """DAG 基本执行：拓扑序执行节点。"""
    wf_id = uuid.uuid4()
    nodes = [
        {"id": "n1", "name": "step1", "agent_id": None},
        {"id": "n2", "name": "step2", "agent_id": None},
    ]
    edges: list[dict[str, Any]] = []

    async def runner(node: dict[str, Any], node_input: str) -> ExecutionResult:
        return ExecutionResult(
            agent_id=None,
            final_answer=f"{node['name']}:{node_input}",
            total_tokens=5,
        )

    result = await execute_workflow_dag(wf_id, nodes, edges, runner, "start")

    assert result.workflow_id == wf_id
    assert "step2" in result.final_answer
    assert "step1" in result.final_answer  # 上下文传递
    assert result.total_tokens > 0


async def test_execute_workflow_dag_empty_nodes_raises() -> None:
    """空节点列表抛 LLMError。"""
    with pytest.raises(LLMError, match="无节点"):
        await execute_workflow_dag(uuid.uuid4(), [], [], lambda *_: None, "input")


async def test_execute_workflow_dag_too_many_nodes_raises() -> None:
    """节点数超 50 抛 LLMError。"""
    nodes = [{"id": f"n{i}", "name": f"s{i}", "agent_id": None} for i in range(51)]
    with pytest.raises(LLMError, match="超 50"):
        await execute_workflow_dag(uuid.uuid4(), nodes, [], lambda *_: None, "input")
