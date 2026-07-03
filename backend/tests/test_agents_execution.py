"""Agent Orchestrator 执行追踪与工作流 DAG eval（agents/SPEC.md Success Criteria）。

覆盖 6 项验收：
1. ReAct 循环在无工具调用时正确终止并返回最终答案
2. 达到 max_turns 仍无最终答案时返回截断提示且 success=True
3. 单工具执行异常被隔离捕获，不中断整体循环
4. DAG 节点 > 50 时创建与执行均报错
5. 每轮 ExecutionTrace 完整记录 thought/action/observation/tokens
6. 工具说明通过 _build_tool_prompt 正确注入 system prompt

直接测 executor 层（AgentExecutor.run / execute_workflow_dag / _build_tool_prompt），
通过注入 mock llm_client.chat 控制每轮 LLM 输出，避免真实网络调用。
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import LLMError, ValidationError
from app.core.llm_client import LLMResponse
from app.domains.agents import service as agent_service
from app.domains.agents.executor import (
    AgentExecutor,
    _build_tool_prompt,
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
    content: str, total_tokens: int = 100
) -> LLMResponse:
    return LLMResponse(
        content=content, usage={"total_tokens": total_tokens}
    )


def _executor_with_chat(
    chat_side_effect: Any, tool_executor: Any = None
) -> AgentExecutor:
    """构造 AgentExecutor，注入 mock chat（AsyncMock side_effect 控制每轮返回）。"""
    llm = AsyncMock()
    llm.chat = AsyncMock(side_effect=chat_side_effect)
    return AgentExecutor(llm, tool_executor=tool_executor)


def _tool_calls_block(name: str, args: dict[str, Any] | None = None) -> str:
    """构造 LLM 输出中的 ```tool_calls``` JSON 块。"""
    import json

    payload = [{"name": name, "args": args or {}}]
    return f"思考一下\n```tool_calls\n{json.dumps(payload, ensure_ascii=False)}\n```"


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


# ===================== 2. max_turns 截断 → success=True =====================


@pytest.mark.asyncio
async def test_max_turns_truncation_returns_success() -> None:
    """达到 max_turns 仍无最终答案 → 截断提示且 success=True（SPEC 2）。"""
    agent = _agent(max_turns=2)
    # 每轮都返回工具调用，永远不给出最终答案
    executor = _executor_with_chat(
        [_llm_response(_tool_calls_block("search"), 50)] * 3
    )

    result = await executor.run(agent, "查询天气")

    assert result.success is True  # 截断也视为 success
    assert "最大轮次" in result.final_answer
    # 应尝试了 max_turns 轮（2 轮）
    assert executor.llm.chat.await_count == 2  # type: ignore[attr-defined]
    assert len(result.traces) == 2


# ===================== 3. 单工具异常隔离 =====================


@pytest.mark.asyncio
async def test_tool_exception_isolated() -> None:
    """单工具执行异常被隔离捕获，记录到 observation 但不中断循环（SPEC 3）。"""
    tool = _tool("search")

    class _FlakyToolExecutor:
        def can_handle(self, tool_type: ToolType) -> bool:
            return True

        async def execute(self, t: ToolDef, args: dict[str, Any]) -> str:
            raise RuntimeError("工具爆炸了")

    agent = _agent(tools=[tool], max_turns=3)
    # 第 1 轮：调用工具 → 异常被隔离；第 2 轮：给出最终答案
    executor = _executor_with_chat(
        [
            _llm_response(_tool_calls_block("search"), 50),
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

    # create_workflow 需要 session 仅在超限时抛错（在 flush 前校验）
    # 这里用最小 mock 验证校验逻辑
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
    """execute_workflow_dag 在节点 > 50 时抛 LLMError（SPEC 4 - 执行路径）。"""
    nodes = [{"id": f"n{i}", "name": f"node-{i}"} for i in range(51)]

    with pytest.raises(LLMError) as exc_info:
        await execute_workflow_dag(
            uuid.uuid4(), nodes, [], AsyncMock(), "input"
        )
    assert "50" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dag_empty_nodes_raises() -> None:
    """DAG 节点为空时抛 LLMError（SPEC Error Cases 补充验证）。"""
    with pytest.raises(LLMError) as exc_info:
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

        async def execute(self, t: ToolDef, args: dict[str, Any]) -> str:
            return "结果=42"

    agent = _agent(tools=[tool], max_turns=3)
    executor = _executor_with_chat(
        [
            _llm_response(_tool_calls_block("calc", {"expr": "6*7"}), 50),
            _llm_response("答案是 42", 30),
        ],
        tool_executor=_RecordingToolExecutor(),
    )

    result = await executor.run(agent, "6*7=?")

    assert len(result.traces) == 2

    # 第 1 轮：有工具调用，应记录 action / observation / thought / tokens
    t1 = result.traces[0]
    assert t1.turn == 1
    assert "calc" in t1.thought  # LLM 原始输出
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


# ===================== 6. _build_tool_prompt 注入 system prompt =====================


def test_build_tool_prompt_injects_tool_descriptions() -> None:
    """_build_tool_prompt 把工具说明格式化注入 system prompt（SPEC 6）。"""
    tools = [
        ToolDef(name="search", type=ToolType.SEARCH, description="web 搜索"),
        ToolDef(name="calc", type=ToolType.CALCULATOR, description="计算器"),
    ]
    prompt = _build_tool_prompt(tools)

    assert "可用工具" in prompt
    assert "```tool_calls" in prompt  # 提示 LLM 输出格式
    # 每个工具都列出（name + type + description）
    assert "search" in prompt and "search" in prompt
    assert "web 搜索" in prompt
    assert "calc" in prompt and "calculator" in prompt
    assert "计算器" in prompt


def test_build_tool_prompt_empty_tools_returns_empty() -> None:
    """无工具时 _build_tool_prompt 返回空串（不污染 system prompt）。"""
    assert _build_tool_prompt([]) == ""


@pytest.mark.asyncio
async def test_tool_prompt_injected_into_system_message() -> None:
    """工具说明被拼接到 system 消息中，Agent 执行时可见（SPEC 6 端到端验证）。"""
    tools = [_tool("search")]
    agent = _agent(tools=tools, max_turns=1)
    captured_messages: list[Any] = []

    async def _capture_chat(messages: list[Any]) -> LLMResponse:
        captured_messages.extend(messages)
        return _llm_response("done", 10)

    executor = _executor_with_chat(_capture_chat)
    await executor.run(agent, "hi")

    # system 消息（第 1 条）应包含工具说明
    system_msg = captured_messages[0]
    assert system_msg.role == "system"
    assert "可用工具" in system_msg.content
    assert "search" in system_msg.content
