"""Agent 执行器 — ReAct 循环实现。

约 120 行。观察 → 思考 → 行动 循环，max_turns 截断。
工具调用从 LLM 输出解析（```tool_calls``` JSON 块）。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from typing import Any, Protocol, cast

from app.core.exceptions import ValidationError
from app.core.llm_client import LLMClient, LLMResponse, Message
from app.domains.agents.models import (
    Agent,
    ExecutionResult,
    ExecutionTrace,
    ToolDef,
    ToolType,
)

logger = logging.getLogger("app.agents.executor")


class ToolExecutor(Protocol):
    """工具执行协议。executor 依赖此协议，具体实现由调用方注入。"""

    def can_handle(self, tool_type: ToolType) -> bool: ...

    async def execute(self, tool: ToolDef, args: dict[str, Any]) -> str: ...


def _build_tool_prompt(tools: list[ToolDef]) -> str:
    """构造工具使用说明，注入 system prompt。"""
    if not tools:
        return ""
    lines = ["", "可用工具：", "调用工具请输出 ```tool_calls``` 代码块，内含 JSON 数组：",
             '[{"name": "tool_name", "args": {...}}]']
    for t in tools:
        lines.append(f"- {t.name} ({t.type.value}): {t.description or '无描述'}")
    return "\n".join(lines)


class AgentExecutor:
    """Agent 执行器，实现 ReAct 循环。"""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self.llm = llm_client
        self.tools = tool_executor

    async def run(
        self,
        agent: Agent,
        user_input: str,
        max_turns: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """执行 Agent。循环直到无工具调用或达到 max_turns。"""
        turns = min(max_turns or agent.max_turns, agent.max_turns)
        tool_defs = [ToolDef(**t) if isinstance(t, dict) else t for t in agent.tools]
        messages = self._init_messages(agent, user_input, context, tool_defs)
        traces: list[ExecutionTrace] = []
        total_tokens = 0
        final_answer = ""
        # success 仅在 LLM 给出最终答案时为 True；达到 max_turns 截断视为失败。
        success = False
        for turn in range(1, turns + 1):
            done, answer = await self._run_turn(
                turn, messages, tool_defs, traces
            )
            if done:
                final_answer = answer
                success = True
                break
        else:
            final_answer = "达到最大轮次仍未给出最终答案。"
        # total_tokens 由 _run_turn 逐轮累积（traces[-1].tokens 即为当前累计值），
        # 此处取末轮值即可，无需在循环内反复覆盖。
        total_tokens = traces[-1].tokens if traces else 0
        return ExecutionResult(
            agent_id=agent.id,
            final_answer=final_answer,
            traces=traces,
            total_tokens=total_tokens,
            success=success,
        )

    def _init_messages(
        self,
        agent: Agent,
        user_input: str,
        context: dict[str, Any] | None,
        tool_defs: list[ToolDef],
    ) -> list[Message]:
        """构造初始消息列表（system + user + 可选 context）。"""
        system = (agent.system_prompt or "You are a helpful assistant.") + _build_tool_prompt(
            tool_defs
        )
        messages = [
            Message(role="system", content=system),
            Message(role="user", content=user_input),
        ]
        if context:
            messages.append(
                Message(role="system", content=f"context: {json.dumps(context)}")
            )
        return messages

    async def _run_turn(
        self,
        turn: int,
        messages: list[Message],
        tool_defs: list[ToolDef],
        traces: list[ExecutionTrace],
    ) -> tuple[bool, str]:
        """执行单轮：调用 LLM → 解析工具调用 → 更新消息与追踪。

        返回 (是否结束, 最终答案)。
        """
        from app.core.llm_client import parse_tool_calls_json

        prev_tokens = traces[-1].tokens if traces else 0
        response: LLMResponse = await self.llm.chat(messages)
        total_tokens = prev_tokens + int(response.usage.get("total_tokens", 0))
        tool_calls = parse_tool_calls_json(response.content)
        if not tool_calls:
            traces.append(ExecutionTrace(
                turn=turn, thought=response.content, tokens=total_tokens
            ))
            return True, response.content
        observation = await self._execute_tools(tool_defs, tool_calls)
        traces.append(ExecutionTrace(
            turn=turn,
            thought=response.content,
            action=json.dumps(tool_calls, ensure_ascii=False),
            observation=observation,
            tokens=total_tokens,
        ))
        messages.append(Message(role="assistant", content=response.content))
        messages.append(Message(role="tool", content=observation))
        return False, ""

    async def _execute_tools(
        self, tool_defs: list[ToolDef], tool_calls: list[dict[str, object]]
    ) -> str:
        """执行一批工具调用，拼接观察结果。"""
        if self.tools is None:
            return "[tool executor 未配置，跳过工具调用]"
        results: list[str] = []
        tool_map = {t.name: t for t in tool_defs}
        for call in tool_calls:
            name = str(call.get("name", ""))
            args = call.get("args", {})
            if not isinstance(args, dict):
                args = {}
            tool = tool_map.get(name)
            if tool is None:
                results.append(f"[未知工具: {name}]")
                continue
            try:
                output = await self.tools.execute(tool, args)
                results.append(f"[{name}] {output}")
            except Exception as exc:  # noqa: BLE001
                # 工具异常拼入 observation 供 LLM 决策（如换工具或放弃），
                # 同时记录完整 traceback 便于排障（编程错误不应被静默吞掉）。
                logger.warning("tool %s execution failed", name, exc_info=True)
                results.append(f"[{name} 错误] {exc}")
        return "\n".join(results)


async def execute_workflow_dag(
    workflow_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    agent_runner: Any,
    entry_input: str,
) -> ExecutionResult:
    """DAG 执行：从 entry 节点沿 edges BFS 遍历，逐节点执行并传递上下文。"""
    if not nodes:
        raise ValidationError("工作流无节点")
    if len(nodes) > 50:
        raise ValidationError("DAG 节点数超 50 上限")
    node_map = {node["id"]: node for node in nodes}
    if edges:
        adjacency: dict[str, list[str]] = {nid: [] for nid in node_map}
        for edge in edges:
            src = edge.get("source")
            tgt = edge.get("target")
            if src in adjacency and tgt in node_map:
                adjacency[src].append(cast(str, tgt))
        entry_id = next(
            (n["id"] for n in nodes if n.get("is_entry")), nodes[0]["id"]
        )
        order: list[str] = []
        visited: set[str] = set()
        queue: deque[str] = deque([entry_id])
        while queue:
            node_id = queue.popleft()
            if node_id in visited:
                continue
            visited.add(node_id)
            order.append(node_id)
            for tgt in adjacency.get(node_id, []):
                if tgt not in visited:
                    queue.append(tgt)
        if not order:
            order = [nodes[0]["id"]]
    else:
        order = [node["id"] for node in nodes]
    context: dict[str, str] = {"__input__": entry_input}
    traces: list[ExecutionTrace] = []
    for idx, node_id in enumerate(order):
        node = node_map[node_id]
        node_input = context.get("__input__", "")
        result = await agent_runner(node, node_input)
        context[node_id] = result.final_answer
        context["__input__"] = result.final_answer
        traces.extend(result.traces)
        traces.append(ExecutionTrace(
            turn=idx + 1, thought=f"node={node['name']}", observation=result.final_answer,
            tokens=result.total_tokens,
        ))
    return ExecutionResult(
        workflow_id=workflow_id,
        final_answer=context["__input__"],
        traces=traces,
        total_tokens=sum(t.tokens for t in traces),
    )
