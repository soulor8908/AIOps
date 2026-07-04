"""agents/executor.py 单元测试 — ReAct 循环（P0-2 原生 function calling）。

使用 mock LLMClient，不依赖真实 LLM API。

覆盖：
- 无工具时直接返回答案
- 有工具调用时执行工具（原生 ToolCall 结构化）
- 多工具并发执行（P2-8）
- 达到最大轮次（P2-8：保留最后输出而非硬失败）
- 工具抛异常时的处理
- context 压缩（P2-8）
- execute_workflow_dag DAG 执行
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import ValidationError
from app.core.llm_client import LLMClient, LLMResponse, Message, ToolCall
from app.domains.agents.executor import (
    AgentExecutor,
    _agent_tools_to_llm_tools,
    execute_workflow_dag,
)
from app.domains.agents.models import (
    Agent,
    ExecutionResult,
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
    """构造按顺序返回不同响应的 mock LLMClient。

    P0-2：chat 签名改为 (messages, tools=None, response_format=None)，
    mock 需接受可选 tools 参数。
    """

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
    result = await executor.run(agent, "hello", context={"key": "value"})

    assert result.final_answer == "ok"


# ===================== 有工具调用（原生 ToolCall） =====================


class _StubToolExecutor:
    """简单的工具执行器 stub。

    P0-2：execute 接收的 tool 参数可能是 _SimpleToolDef（executor 内部构造），
    用 getattr 兼容。
    """

    def __init__(self, result_map: dict[str, str] | None = None) -> None:
        self.result_map = result_map or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def can_handle(self, tool_type: ToolType) -> bool:
        return True

    async def execute(self, tool: Any, args: dict[str, Any]) -> str:
        name = getattr(tool, "name", str(tool))
        self.calls.append((name, args))
        return self.result_map.get(name, f"result for {name}")


async def test_executor_with_tool_call() -> None:
    """有工具调用时执行工具（P0-2 原生 ToolCall 结构化）。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator", "description": "计算器"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        # 第一轮：返回原生 tool_calls
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", name="calc", args={"expr": "1+1"})],
            usage={"total_tokens": 5},
        ),
        # 第二轮：返回最终答案
        LLMResponse(content="最终结果 2", usage={"total_tokens": 5}),
    ])

    tool_exec = _StubToolExecutor({"calc": "计算结果: 2"})
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "算 1+1")

    assert "最终结果" in result.final_answer
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
    """单轮多个工具调用（P2-8 并发执行）。"""
    agent = _make_agent(
        tools=[
            {"name": "search", "type": "search"},
            {"name": "calc", "type": "calculator"},
        ],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="t1", name="search", args={"query": "test"}),
                ToolCall(id="t2", name="calc", args={"expr": "2+2"}),
            ],
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
    # 两个工具都被调用（并发不改变调用计数）
    assert len(tool_exec.calls) == 2
    called_names = {c[0] for c in tool_exec.calls}
    assert called_names == {"search", "calc"}
    # 观察结果包含两个工具的输出
    assert "found: hello" in result.traces[0].observation
    assert "result: 4" in result.traces[0].observation


# ===================== 最大轮次 =====================


async def test_executor_max_turns_reached() -> None:
    """达到最大轮次（P2-8：保留最后输出而非硬失败消息）。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=2,
    )
    # LLM 始终返回工具调用，不给出最终答案
    mock_llm = _make_mock_llm([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", name="calc", args={})],
            usage={"total_tokens": 5},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t2", name="calc", args={})],
            usage={"total_tokens": 5},
        ),
    ])

    tool_exec = _StubToolExecutor({"calc": "result"})
    executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
    result = await executor.run(agent, "loop")

    # P2-8：截断保留最后输出，不硬判失败消息
    assert result.success is False
    assert len(result.traces) == 2
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


# ===================== 工具异常 =====================


class _ErrorToolExecutor:
    """总是抛异常的工具执行器。"""

    def can_handle(self, tool_type: ToolType) -> bool:
        return True

    async def execute(self, tool: Any, args: dict[str, Any]) -> str:
        raise RuntimeError("tool crashed")


async def test_executor_tool_exception() -> None:
    """工具抛异常时（P2-8 并发 + return_exceptions）拼入 observation。"""
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )
    mock_llm = _make_mock_llm([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", name="calc", args={"x": 1})],
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
            content="",
            tool_calls=[ToolCall(id="t1", name="calc", args={})],
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
            content="",
            tool_calls=[ToolCall(id="t1", name="calc", args={})],
            usage={"total_tokens": 10},
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t2", name="calc", args={})],
            usage={"total_tokens": 20},
        ),
        LLMResponse(content="done", usage={"total_tokens": 5}),
    ])

    executor = AgentExecutor(mock_llm, tool_executor=_StubToolExecutor())
    result = await executor.run(agent, "test")

    # total_tokens = 10 + 20 + 5 = 35
    assert result.total_tokens == 35


# ===================== _agent_tools_to_llm_tools（P0-2） =====================


def test_agent_tools_to_llm_tools_search() -> None:
    """search 工具生成 query 参数 schema。"""
    tools = [{"name": "search", "type": "search", "description": "搜索"}]
    llm_tools = _agent_tools_to_llm_tools(tools)
    assert len(llm_tools) == 1
    assert llm_tools[0].name == "search"
    assert "query" in llm_tools[0].parameters["properties"]
    assert llm_tools[0].parameters["required"] == ["query"]


def test_agent_tools_to_llm_tools_calculator() -> None:
    """calculator 工具生成 expr 参数 schema。"""
    tools = [{"name": "calc", "type": "calculator"}]
    llm_tools = _agent_tools_to_llm_tools(tools)
    assert llm_tools[0].parameters["required"] == ["expr"]


def test_agent_tools_to_llm_tools_custom() -> None:
    """custom 工具透传 config 字段为 properties。"""
    tools = [{"name": "mytool", "type": "custom", "config": {"url": "x", "method": "GET"}}]
    llm_tools = _agent_tools_to_llm_tools(tools)
    assert "url" in llm_tools[0].parameters["properties"]
    assert "method" in llm_tools[0].parameters["properties"]


def test_agent_tools_to_llm_tools_skips_empty_name() -> None:
    """无 name 的工具被跳过。"""
    tools = [{"name": "", "type": "custom"}]
    assert _agent_tools_to_llm_tools(tools) == []


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
    """空节点列表抛 ValidationError（输入校验，非 LLM 调用失败）。"""
    with pytest.raises(ValidationError, match="无节点"):
        await execute_workflow_dag(uuid.uuid4(), [], [], lambda *_: None, "input")


async def test_execute_workflow_dag_too_many_nodes_raises() -> None:
    """节点数超 50 抛 ValidationError（输入校验，非 LLM 调用失败）。"""
    nodes = [{"id": f"n{i}", "name": f"s{i}", "agent_id": None} for i in range(51)]
    with pytest.raises(ValidationError, match="超 50"):
        await execute_workflow_dag(uuid.uuid4(), nodes, [], lambda *_: None, "input")
