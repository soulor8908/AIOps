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
from app.core.llm_client import (
    LLMClient,
    LLMResponse,
    Message,
    StreamEvent,
    ToolCall,
)
from app.domains.agents.executor import (
    AgentDelegateExecutor,
    AgentExecutor,
    _agent_tools_to_llm_tools,
    _SimpleToolDef,
    _tool_parameters,
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
    self_eval: bool = False,
    self_heal: bool = False,
    self_eval_threshold: float = 0.7,
    self_heal_max_retries: int = 1,
) -> Agent:
    """构造无需 DB 的 Agent 实例。"""
    return Agent(
        id=uuid.uuid4(),
        name=name,
        system_prompt=system_prompt,
        tools=tools or [],
        max_turns=max_turns,
        self_eval=self_eval,
        self_heal=self_heal,
        self_eval_threshold=self_eval_threshold,
        self_heal_max_retries=self_heal_max_retries,
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


# ===================== P2-9 工具调用指标采集 =====================


async def test_executor_records_tool_call_metrics_on_success() -> None:
    """P2-9：工具调用成功时记录 tool_calls{tool_name}，不记 tool_errors。"""
    from app.core.metrics import metrics as registry

    registry.reset()
    try:
        agent = _make_agent(
            tools=[{"name": "search", "type": "search"}],
            max_turns=5,
        )
        mock_llm = _make_mock_llm([
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="search", args={"query": "q"})],
                usage={"total_tokens": 5},
            ),
            LLMResponse(content="done", usage={"total_tokens": 2}),
        ])
        tool_exec = _StubToolExecutor({"search": "found"})
        executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
        await executor.run(agent, "test")

        assert registry.get_counter("tool_calls", ("search",)) == 1.0
        assert registry.get_counter_sum("tool_errors") == 0.0
    finally:
        registry.reset()


async def test_executor_records_tool_error_metrics_on_exception() -> None:
    """P2-9：工具调用抛异常时记 tool_calls + tool_errors{tool_name,error_type}。

    error_type 为异常类名（RuntimeError），用于失败模式聚类。
    """
    from app.core.metrics import metrics as registry

    registry.reset()
    try:
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
            LLMResponse(content="recovered", usage={"total_tokens": 2}),
        ])
        executor = AgentExecutor(mock_llm, tool_executor=_ErrorToolExecutor())
        await executor.run(agent, "test")

        # 工具调用计数（无论成功失败）
        assert registry.get_counter("tool_calls", ("calc",)) == 1.0
        # 工具失败计数，error_type 为异常类名
        assert registry.get_counter("tool_errors", ("calc", "RuntimeError")) == 1.0
    finally:
        registry.reset()


async def test_executor_records_metrics_for_multiple_tool_calls() -> None:
    """P2-9：单轮多个工具调用时每个工具都记 tool_calls。"""
    from app.core.metrics import metrics as registry

    registry.reset()
    try:
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
                    ToolCall(id="t1", name="search", args={"query": "q"}),
                    ToolCall(id="t2", name="calc", args={"expr": "1+1"}),
                ],
                usage={"total_tokens": 8},
            ),
            LLMResponse(content="done", usage={"total_tokens": 2}),
        ])
        tool_exec = _StubToolExecutor({"search": "s", "calc": "c"})
        executor = AgentExecutor(mock_llm, tool_executor=tool_exec)
        await executor.run(agent, "test")

        assert registry.get_counter("tool_calls", ("search",)) == 1.0
        assert registry.get_counter("tool_calls", ("calc",)) == 1.0
        assert registry.get_counter_sum("tool_calls") == 2.0
    finally:
        registry.reset()


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


# ===================== P3-11：自主运维（自评 + 自愈合） =====================


async def test_self_eval_passing_skips_heal() -> None:
    """P3-11：self_eval 开启但自评达标时，不触发自愈合。

    mock 顺序：generation(答案) → judge(score=0.9 ≥ 0.7 阈值)。
    heal_attempts=0，eval_score=0.9，final_answer 不变。
    """
    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            return LLMResponse(content="答案是 42", usage={"total_tokens": 10})
        # judge 调用
        return LLMResponse(
            content='{"score": 0.9, "reason": "ok"}', usage={"total_tokens": 3}
        )

    agent = _make_agent(self_eval=True, self_heal=True, self_eval_threshold=0.7)
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")

    assert result.final_answer == "答案是 42"
    assert result.eval_score == 0.9
    assert result.heal_attempts == 0


async def test_self_heal_retries_until_passing() -> None:
    """P3-11：自评不达标时自愈合重试，重试后达标则停止。

    mock 顺序：
    1. generation → "差答案"
    2. judge → score=0.3（不达标）
    3. heal generation → "好答案"
    4. judge → score=0.95（达标，停止）
    """
    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = call_idx["n"]
        call_idx["n"] += 1
        # generation 调用无 response_format，judge 调用带 response_format
        if response_format is not None:
            # judge 调用
            if idx == 1:
                return LLMResponse(
                    content='{"score": 0.3, "reason": "不完整"}',
                    usage={"total_tokens": 3},
                )
            return LLMResponse(
                content='{"score": 0.95, "reason": "完整"}',
                usage={"total_tokens": 3},
            )
        # generation 调用
        if idx == 0:
            return LLMResponse(content="差答案", usage={"total_tokens": 5})
        return LLMResponse(content="好答案", usage={"total_tokens": 5})

    agent = _make_agent(
        self_eval=True, self_heal=True,
        self_eval_threshold=0.7, self_heal_max_retries=2,
    )
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")

    assert result.final_answer == "好答案"
    assert result.eval_score == 0.95
    assert result.heal_attempts == 1


async def test_self_heal_exhausts_retries_keeps_last_answer() -> None:
    """P3-11：自愈合重试耗尽仍不达标，保留最后一次答案。

    mock：generation → judge(0.3) → heal gen → judge(0.4) → heal gen → judge(0.5)
    阈值 0.7，max_retries=2，最终保留第二次 heal 的答案。
    """
    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = call_idx["n"]
        call_idx["n"] += 1
        if response_format is not None:
            # judge：始终返回低分
            return LLMResponse(
                content='{"score": 0.4, "reason": "仍不达标"}',
                usage={"total_tokens": 2},
            )
        # generation：首次差答案，后续改进答案
        if idx == 0:
            return LLMResponse(content="差答案", usage={"total_tokens": 5})
        return LLMResponse(content=f"改进答案v{idx}", usage={"total_tokens": 5})

    agent = _make_agent(
        self_eval=True, self_heal=True,
        self_eval_threshold=0.7, self_heal_max_retries=2,
    )
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")

    # 重试耗尽，保留最后一次 heal 的答案
    assert "改进答案" in result.final_answer
    assert result.heal_attempts == 2
    assert result.eval_score is not None and result.eval_score < 0.7


async def test_self_eval_disabled_skips_judge() -> None:
    """P3-11：self_eval=False 时不调用 judge，eval_score 为 None。"""
    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        call_idx["n"] += 1
        return LLMResponse(content="答案", usage={"total_tokens": 5})

    agent = _make_agent(self_eval=False)
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")

    assert result.eval_score is None
    assert result.eval_reason is None
    assert result.heal_attempts == 0
    assert call_idx["n"] == 1  # 仅一次 generation 调用，无 judge


async def test_self_eval_judge_failure_degrades_gracefully() -> None:
    """P3-11：judge 调用抛异常时降级，不阻塞主流程，eval_score=None。"""
    from app.core.exceptions import LLMError

    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            return LLMResponse(content="答案", usage={"total_tokens": 5})
        # judge 调用抛 LLMError
        raise LLMError("judge API 不可用")

    agent = _make_agent(self_eval=True, self_heal=True)
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")

    # 降级：原答案保留，eval_score=None，无 heal
    assert result.final_answer == "答案"
    assert result.eval_score is None
    assert result.heal_attempts == 0


# ===================== P3-12：multi-agent A2A =====================


def test_tool_parameters_agent_delegate_has_input_param() -> None:
    """P3-12：agent_delegate 工具生成 input 参数 schema。"""
    params = _tool_parameters("ask_expert", "agent_delegate", {"agent_id": "x"})
    assert params["required"] == ["input"]
    assert "input" in params["properties"]


def test_agent_tools_to_llm_tools_agent_delegate() -> None:
    """P3-12：agent_delegate 工具转为 LLM ToolDef，含 input 参数。"""
    tools = [{
        "name": "ask_researcher",
        "type": "agent_delegate",
        "description": "委托研究员",
        "config": {"agent_id": "abc-123"},
    }]
    llm_tools = _agent_tools_to_llm_tools(tools)
    assert len(llm_tools) == 1
    assert llm_tools[0].name == "ask_researcher"
    assert "input" in llm_tools[0].parameters["properties"]


async def test_agent_delegate_executor_routes_delegate_to_runner() -> None:
    """P3-12：AgentDelegateExecutor 把 agent_delegate 工具调用路由到 runner。"""
    target_id = uuid.uuid4()
    agent_tools = [{
        "name": "ask_expert",
        "type": "agent_delegate",
        "config": {"agent_id": str(target_id)},
    }]
    runner_calls: list[tuple[uuid.UUID, str]] = []

    async def runner(aid: uuid.UUID, inp: str) -> str:
        runner_calls.append((aid, inp))
        return f"专家回答: {inp}"

    delegate_exec = AgentDelegateExecutor(agent_tools, runner)
    tool_def = _SimpleToolDef(name="ask_expert")
    result = await delegate_exec.execute(tool_def, {"input": "什么是 RAG?"})

    assert result == "专家回答: 什么是 RAG?"
    assert runner_calls == [(target_id, "什么是 RAG?")]


async def test_agent_delegate_executor_falls_back_to_inner() -> None:
    """P3-12：非 delegate 工具转交 inner ToolExecutor。"""

    class InnerExecutor:
        async def execute(self, tool: Any, args: dict[str, Any]) -> str:
            return f"inner handled {tool.name}"

        def can_handle(self, tool_type: ToolType) -> bool:
            return tool_type != ToolType.AGENT_DELEGATE

    delegate_exec = AgentDelegateExecutor(
        agent_tools=[{"name": "calc", "type": "calculator"}],
        agent_runner=lambda *_: None,
        inner=InnerExecutor(),
    )
    tool_def = _SimpleToolDef(name="calc")
    result = await delegate_exec.execute(tool_def, {"expr": "1+1"})
    assert result == "inner handled calc"


async def test_agent_delegate_executor_unknown_tool_no_inner() -> None:
    """P3-12：未知工具且无 inner 时返回提示，不抛异常。"""
    delegate_exec = AgentDelegateExecutor(
        agent_tools=[], agent_runner=lambda *_: None
    )
    tool_def = _SimpleToolDef(name="unknown")
    result = await delegate_exec.execute(tool_def, {})
    assert "无可用执行器" in result


async def test_agent_delegate_executor_runner_failure_returns_error_obs() -> None:
    """P3-12：runner 抛异常时返回错误观察，不阻塞主循环。"""
    target_id = uuid.uuid4()
    agent_tools = [{
        "name": "ask_expert",
        "type": "agent_delegate",
        "config": {"agent_id": str(target_id)},
    }]

    async def runner(aid: uuid.UUID, inp: str) -> str:
        raise RuntimeError("目标 Agent 不可用")

    delegate_exec = AgentDelegateExecutor(agent_tools, runner)
    tool_def = _SimpleToolDef(name="ask_expert")
    result = await delegate_exec.execute(tool_def, {"input": "问题"})
    assert "委托失败" in result
    assert "目标 Agent 不可用" in result


async def test_executor_invokes_delegate_tool_end_to_end() -> None:
    """P3-12：Agent A 通过 agent_delegate 工具调用 Agent B，B 的答案成为 A 的观察。

    mock LLM 顺序：
    1. A 首轮 → tool_call(ask_expert, {input: "什么是 RAG?"})
    2. A 次轮 → 最终答案（基于 B 的观察）
    delegate runner 返回 B 的 final_answer。
    """
    target_id = uuid.uuid4()
    agent_a = _make_agent(
        name="orchestrator",
        tools=[{
            "name": "ask_expert",
            "type": "agent_delegate",
            "config": {"agent_id": str(target_id)},
        }],
        max_turns=5,
    )

    async def runner(aid: uuid.UUID, inp: str) -> str:
        assert aid == target_id
        return f"B 的回答：{inp} 是检索增强生成"

    call_idx = {"n": 0}

    async def chat_seq(
        messages: list[Message],
        tools: list[Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="t1", name="ask_expert", args={"input": "什么是 RAG?"})],
                usage={"total_tokens": 5},
            )
        # 次轮：基于观察给出最终答案
        return LLMResponse(content="RAG 是检索增强生成", usage={"total_tokens": 3})

    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.chat = chat_seq
    mock_llm.close = AsyncMock()
    delegate_exec = AgentDelegateExecutor(agent_a.tools, runner)
    executor = AgentExecutor(mock_llm, tool_executor=delegate_exec)
    result = await executor.run(agent_a, "解释 RAG")

    assert "RAG" in result.final_answer
    assert result.traces[0].observation == "[ask_expert] B 的回答：什么是 RAG? 是检索增强生成"
    assert result.success is True


# ===================== P6e：真 streaming（stream_chat_events） =====================


def _make_events_streaming_mock_llm(
    event_sequences: list[list[StreamEvent]],
) -> Any:
    """构造支持 stream_chat_events 的 mock LLMClient（P6e 真 streaming）。

    event_sequences 为每轮的 StreamEvent 列表，按调用顺序消费。
    run_stream 每轮调一次 stream_chat_events，依次取 event_sequences[0]、[1]...
    """
    call_idx = {"n": 0}

    async def fake_stream_events(
        messages: list[Message], tools: list[Any] | None = None
    ) -> Any:
        idx = min(call_idx["n"], len(event_sequences) - 1)
        call_idx["n"] += 1
        for ev in event_sequences[idx]:
            yield ev

    stub = MagicMock(spec=LLMClient)
    stub.stream_chat_events = fake_stream_events
    stub.close = AsyncMock()
    return stub


async def test_run_stream_yields_tokens_for_final_answer() -> None:
    """P6e：无工具时 run_stream 单流逐 token yield 最终答案 + done 事件。"""
    agent = _make_agent(system_prompt="回答问题", max_turns=3)
    mock_llm = _make_events_streaming_mock_llm([
        [
            StreamEvent(type="text", content="答案"),
            StreamEvent(type="text", content="是"),
            StreamEvent(type="text", content=" "),
            StreamEvent(type="text", content="42"),
            StreamEvent(
                type="finish", content="答案是 42", usage={"total_tokens": 10}
            ),
        ],
    ])
    executor = AgentExecutor(mock_llm)
    events = [e async for e in executor.run_stream(agent, "问题")]

    # 4 个 token 事件 + 1 个 done 事件
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    assert len(token_events) == 4
    assert "".join(e["content"] for e in token_events) == "答案是 42"
    assert len(done_events) == 1
    result = done_events[0]["result"]
    assert result.final_answer == "答案是 42"
    assert result.success is True


async def test_run_stream_yields_tool_and_observation_events() -> None:
    """P6e：工具调用轮 yield tool + observation 事件，最终轮 yield token。

    单流消费：第 1 轮 stream_chat_events 产出 tool_call + finish（含 tool_calls），
    第 2 轮产出 text + finish（无 tool_calls = 最终答案）。每轮只调一次 LLM。
    """
    agent = _make_agent(
        tools=[{"name": "calc", "type": "calculator"}],
        max_turns=5,
    )

    class StubTool:
        async def execute(self, tool: Any, args: dict[str, Any]) -> str:
            return "计算结果: 2"

        def can_handle(self, tool_type: ToolType) -> bool:
            return True

    mock_llm = _make_events_streaming_mock_llm([
        # 第 1 轮：工具调用（tool_call 增量 + finish 给完整 tool_calls）
        [
            StreamEvent(
                type="tool_call",
                tool_call_id="t1",
                tool_call_name="calc",
                args_delta='{"expr":"1+1"}',
            ),
            StreamEvent(
                type="finish",
                tool_calls=[ToolCall(id="t1", name="calc", args={"expr": "1+1"})],
                usage={"total_tokens": 5},
            ),
        ],
        # 第 2 轮：最终答案（text 逐 token + finish 无 tool_calls）
        [
            StreamEvent(type="text", content="最终"),
            StreamEvent(type="text", content="答案"),
            StreamEvent(type="text", content=" "),
            StreamEvent(type="text", content="2"),
            StreamEvent(
                type="finish", content="最终答案 2", usage={"total_tokens": 3}
            ),
        ],
    ])
    executor = AgentExecutor(mock_llm, tool_executor=StubTool())
    events = [e async for e in executor.run_stream(agent, "算 1+1")]

    tool_events = [e for e in events if e["type"] == "tool"]
    obs_events = [e for e in events if e["type"] == "observation"]
    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]

    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "calc"
    assert len(obs_events) == 1
    assert "计算结果: 2" in obs_events[0]["content"]
    assert len(token_events) == 4
    assert "".join(e["content"] for e in token_events) == "最终答案 2"
    assert len(done_events) == 1
    result = done_events[0]["result"]
    assert result.success is True
    assert len(result.traces) == 2  # 工具轮 + 最终答案轮


async def test_run_stream_stream_failure_degrades_to_collected_text() -> None:
    """P6e：stream_chat_events 抛异常时降级用已收集文本，不阻塞流。"""
    agent = _make_agent(system_prompt="回答", max_turns=3)
    mock_llm = MagicMock(spec=LLMClient)

    async def failing_stream_events(
        messages: list[Message], tools: list[Any] | None = None
    ) -> Any:
        # 先 yield 部分 text，再抛异常
        yield StreamEvent(type="text", content="部分")
        raise RuntimeError("stream 中断")
        yield  # unreachable — 让它成为 async generator

    mock_llm.stream_chat_events = failing_stream_events
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    events = [e async for e in executor.run_stream(agent, "问题")]

    token_events = [e for e in events if e["type"] == "token"]
    done_events = [e for e in events if e["type"] == "done"]
    # 降级：已 yield 的 token 保留，done 事件含收集到的文本
    assert len(token_events) == 1
    assert token_events[0]["content"] == "部分"
    assert len(done_events) == 1
    assert done_events[0]["result"].final_answer == "部分"
    assert done_events[0]["result"].success is True  # 有文本即视为成功降级


async def test_run_stream_stream_failure_no_text_yields_success_false() -> None:
    """P6e：stream 失败且无已收集文本时，done 事件 success=False。"""
    agent = _make_agent(system_prompt="回答", max_turns=3)
    mock_llm = MagicMock(spec=LLMClient)

    async def failing_stream_events(
        messages: list[Message], tools: list[Any] | None = None
    ) -> Any:
        raise RuntimeError("stream 立即失败")
        yield  # unreachable — 让它成为 async generator

    mock_llm.stream_chat_events = failing_stream_events
    mock_llm.close = AsyncMock()
    executor = AgentExecutor(mock_llm)
    events = [e async for e in executor.run_stream(agent, "问题")]

    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    assert done_events[0]["result"].success is False
    assert done_events[0]["result"].final_answer == ""


async def test_run_stream_max_turns_yields_done_with_success_false() -> None:
    """P6e：达到 max_turns 仍未给出最终答案时，done 事件 success=False。"""
    agent = _make_agent(tools=[{"name": "calc", "type": "calculator"}], max_turns=2)

    class StubTool:
        async def execute(self, tool: Any, args: dict[str, Any]) -> str:
            return "结果"

        def can_handle(self, tool_type: ToolType) -> bool:
            return True

    # 每轮 stream 都返回 tool_call，永不给最终答案
    mock_llm = _make_events_streaming_mock_llm([
        [
            StreamEvent(
                type="finish",
                tool_calls=[ToolCall(id="t1", name="calc", args={"expr": "1"})],
                usage={"total_tokens": 2},
            ),
        ],
    ])
    executor = AgentExecutor(mock_llm, tool_executor=StubTool())
    events = [e async for e in executor.run_stream(agent, "问题")]

    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    result = done_events[0]["result"]
    assert result.success is False
