"""Agent 执行器 — ReAct 循环实现（P0-2 原生 function calling）。

约 130 行。观察 → 思考 → 行动 循环，max_turns 截断。

P0-2 之前靠解析 ```tool_calls``` 文本块（脆弱，LLM 多一个空格就崩）。
P0-2 起用原生 function calling：OpenAI tools / Anthropic tool_use API，
工具调用由 provider 结构化返回，executor 直接拿到 ToolCall 列表。

P2-8：多工具并发执行（asyncio.gather）+ context 压缩（达阈值摘要）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.core.exceptions import ValidationError
from app.core.llm_client import LLMClient, LLMResponse, Message, ToolCall, ToolDef
from app.domains.agents.models import (
    Agent,
    ExecutionResult,
    ExecutionTrace,
    ToolType,
)

logger = logging.getLogger("app.agents.executor")

# context 压缩阈值（P2-8）：超过则用 LLM 摘要历史，避免超 context window
_CONTEXT_COMPRESS_THRESHOLD = 20


class ToolExecutor(Protocol):
    """工具执行协议。executor 依赖此协议，具体实现由调用方注入。"""

    def can_handle(self, tool_type: ToolType) -> bool: ...

    async def execute(self, tool: Any, args: dict[str, Any]) -> str: ...


def _agent_tools_to_llm_tools(tools: list[Any]) -> list[ToolDef]:
    """把 Agent.tools（dict / ToolDef）转为 LLMClient 的 ToolDef。

    Agent ORM 的 tools 字段是无 schema 的 JSONB（仅 name/type/description/config），
    LLM 原生 function calling 需要显式 parameters JSON Schema。这里按 ToolType
    生成最小 schema：search/rag 一个 query 参数，calculator 一个 expr 参数，
    http/code/custom 透传 config 作为 properties。
    """
    llm_tools: list[ToolDef] = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name", "")
            ttype = t.get("type", "custom")
            desc = t.get("description") or ""
            config = t.get("config", {}) or {}
        else:
            name = t.name
            ttype = t.type.value if hasattr(t.type, "value") else str(t.type)
            desc = t.description or ""
            config = t.config
        if not name:
            continue
        params = _tool_parameters(name, ttype, config)
        llm_tools.append(ToolDef(name=name, description=desc or name, parameters=params))
    return llm_tools


def _tool_parameters(name: str, ttype: str, config: dict[str, Any]) -> dict[str, Any]:
    """按工具类型生成最小 JSON Schema。"""
    if ttype in ("search", "rag"):
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索查询"}},
            "required": ["query"],
        }
    if ttype == "calculator":
        return {
            "type": "object",
            "properties": {"expr": {"type": "string", "description": "数学表达式"}},
            "required": ["expr"],
        }
    if ttype == "http":
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
            },
            "required": ["url"],
        }
    if ttype == "code":
        return {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        }
    if ttype == "agent_delegate":
        # P3-12：A2A 委托工具。input 为传给目标 Agent 的问题。
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "传给目标 Agent 的问题"}
            },
            "required": ["input"],
        }
    # custom：透传 config 字段为 properties
    props = {k: {"type": "string"} for k in config}
    return {"type": "object", "properties": props or {}}


class AgentExecutor:
    """Agent 执行器，实现 ReAct 循环（P0-2 原生 function calling）。"""

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
        """执行 Agent。循环直到无工具调用或达到 max_turns。

        P3-11：若 ``agent.self_eval`` 为真，最终答案产出后用 LLM judge 自评。
        若 ``agent.self_heal`` 为真且自评不达标，追加反馈消息重跑（最多
        ``self_heal_max_retries`` 次）。自评失败不阻塞主流程，降级返回原答案。
        """
        turns = min(max_turns or agent.max_turns, agent.max_turns)
        # P0-2：把 Agent.tools 转为 LLMClient 原生 ToolDef
        llm_tools = _agent_tools_to_llm_tools(agent.tools)
        messages = self._init_messages(agent, user_input, context)
        traces: list[ExecutionTrace] = []
        final_answer = ""
        # success 仅在 LLM 给出最终答案时为 True；达到 max_turns 截断视为未完成。
        # P2-8：截断不再硬判失败，标记 success=False 但 final_answer 保留最后输出。
        success = False
        for turn in range(1, turns + 1):
            # P2-8：context 压缩，避免长任务超 context window
            if len(messages) > _CONTEXT_COMPRESS_THRESHOLD:
                messages = await self._compress_context(messages)
            done, answer = await self._run_turn(
                turn, messages, llm_tools, traces
            )
            if done:
                final_answer = answer
                success = True
                break
        else:
            final_answer = messages[-1].content if messages else "达到最大轮次未给出最终答案。"
        total_tokens = traces[-1].tokens if traces else 0

        # P3-11：自主运维 — 自评 + 自愈合
        eval_score: float | None = None
        eval_reason: str | None = None
        heal_attempts = 0
        if getattr(agent, "self_eval", False):
            eval_score, eval_reason, healed_answer, healed_tokens, heal_attempts = (
                await self._self_eval_and_heal(
                    agent, user_input, final_answer, messages, llm_tools, traces
                )
            )
            if healed_answer is not None:
                final_answer = healed_answer
                total_tokens = healed_tokens
                success = True
        return ExecutionResult(
            agent_id=agent.id,
            final_answer=final_answer,
            traces=traces,
            total_tokens=total_tokens,
            success=success,
            eval_score=eval_score,
            eval_reason=eval_reason,
            heal_attempts=heal_attempts,
        )

    async def _self_eval_and_heal(
        self,
        agent: Agent,
        user_input: str,
        initial_answer: str,
        messages: list[Message],
        llm_tools: list[ToolDef],
        traces: list[ExecutionTrace],
    ) -> tuple[float | None, str | None, str | None, int, int]:
        """P3-11：自评答案质量，不达标则自愈合重试。

        返回 (eval_score, eval_reason, healed_answer, healed_tokens, heal_attempts)。
        - healed_answer 为 None 表示未愈合（自评已达标或自愈合关闭/耗尽）
        - 自评调用失败时降级：返回 (None, None, None, 0, 0)，不阻塞主流程
        """
        from app.domains.evals.judge import judge_llm

        threshold = float(getattr(agent, "self_eval_threshold", 0.7))
        max_retries = (
            int(getattr(agent, "self_heal_max_retries", 0))
            if getattr(agent, "self_heal", False)
            else 0
        )
        current_answer = initial_answer
        heal_attempts = 0
        try:
            judge_result = await judge_llm(
                actual=current_answer,
                expected=user_input,
                client=self.llm,
                criteria="回答是否准确、相关且完整地回应了用户问题",
            )
        except Exception:  # noqa: BLE001
            logger.warning("self-eval judge 调用失败，跳过自愈合")
            return None, None, None, 0, 0
        eval_score = judge_result.score
        eval_reason = judge_result.reason
        if eval_score >= threshold or max_retries <= 0:
            return eval_score, eval_reason, None, 0, 0
        # 自愈合：追加反馈消息重跑
        for _ in range(max_retries):
            heal_attempts += 1
            feedback = (
                f"上一轮回答质量自评不达标（score={eval_score:.2f}, "
                f"threshold={threshold:.2f}，原因：{eval_reason}）。"
                f"请改进回答，更准确、相关且完整地回应问题。"
            )
            messages.append(Message(role="assistant", content=current_answer))
            messages.append(Message(role="user", content=feedback))
            done, answer = await self._run_turn(
                len(traces) + 1, messages, llm_tools, traces
            )
            if not done:
                break
            current_answer = answer
            try:
                judge_result = await judge_llm(
                    actual=current_answer,
                    expected=user_input,
                    client=self.llm,
                    criteria="回答是否准确、相关且完整地回应了用户问题",
                )
            except Exception:  # noqa: BLE001
                logger.warning("self-heal judge 调用失败，保留当前答案")
                break
            eval_score = judge_result.score
            eval_reason = judge_result.reason
            if eval_score >= threshold:
                break
        total_tokens = traces[-1].tokens if traces else 0
        return eval_score, eval_reason, current_answer, total_tokens, heal_attempts

    def _init_messages(
        self,
        agent: Agent,
        user_input: str,
        context: dict[str, Any] | None,
    ) -> list[Message]:
        """构造初始消息列表（system + user + 可选 context）。

        P2-10：system prompt 标记 cache_control=True 启用 prompt caching，
        多轮对话复用 system 段省 50%+ 成本。
        """
        system = agent.system_prompt or "You are a helpful assistant."
        messages = [
            Message(role="system", content=system, cache_control=True),
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
        llm_tools: list[ToolDef],
        traces: list[ExecutionTrace],
    ) -> tuple[bool, str]:
        """执行单轮：调用 LLM（原生 tools）→ 执行工具 → 更新消息与追踪。

        返回 (是否结束, 最终答案)。
        """
        prev_tokens = traces[-1].tokens if traces else 0
        response: LLMResponse = await self.llm.chat(messages, tools=llm_tools or None)
        total_tokens = prev_tokens + int(response.usage.get("total_tokens", 0))
        # P0-2：原生 function calling，工具调用结构化返回
        if not response.tool_calls:
            traces.append(ExecutionTrace(
                turn=turn, thought=response.content, tokens=total_tokens
            ))
            return True, response.content
        observation = await self._execute_tools(response.tool_calls)
        traces.append(ExecutionTrace(
            turn=turn,
            thought=response.content,
            action=json.dumps(
                [{"name": c.name, "args": c.args} for c in response.tool_calls],
                ensure_ascii=False,
            ),
            observation=observation,
            tokens=total_tokens,
        ))
        messages.append(Message(role="assistant", content=response.content))
        messages.append(Message(role="tool", content=observation))
        return False, ""

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> str:
        """执行一批工具调用（P2-8 并发），拼接观察结果。

        串行 → asyncio.gather 并发，多工具调用延迟从 N×T 降到 max(T)。
        """
        if self.tools is None:
            return "[tool executor 未配置，跳过工具调用]"
        # 并发执行所有工具调用
        results = await asyncio.gather(
            *(self._execute_single(tc) for tc in tool_calls),
            return_exceptions=True,
        )
        lines: list[str] = []
        for tc, res in zip(tool_calls, results, strict=True):
            if isinstance(res, Exception):
                logger.warning("tool %s execution failed", tc.name, exc_info=res)
                lines.append(f"[{tc.name} 错误] {res}")
            else:
                lines.append(f"[{tc.name}] {res}")
        return "\n".join(lines)

    async def _execute_single(self, tc: ToolCall) -> str:
        """执行单个工具调用。"""
        # Agent.tools 是 JSONB dict，tool_executor 协议接收 ToolDef-like 对象。
        # 这里构造一个轻量对象满足 execute 签名。
        tool_def = _SimpleToolDef(name=tc.name)
        return await self.tools.execute(tool_def, tc.args)  # type: ignore[union-attr]

    async def _compress_context(self, messages: list[Message]) -> list[Message]:
        """P2-8：context 压缩。保留 system + 最近若干轮，中间用 LLM 摘要替代。

        避免 Agent 长任务到 max_turns 因超 context window 失败。
        摘要失败时降级为简单截断（保留首尾），不阻塞主流程。
        """
        if len(messages) <= _CONTEXT_COMPRESS_THRESHOLD:
            return messages
        # 保留首条 system + 最后 6 条，中间摘要素
        head = messages[:1]
        middle = messages[1:-6]
        tail = messages[-6:]
        summary_text = "\n".join(f"[{m.role}] {m.content[:200]}" for m in middle)
        try:
            resp = await self.llm.chat([
                Message(role="system", content="Summarize the conversation so far concisely."),
                Message(role="user", content=summary_text),
            ])
            summary_msg = Message(role="system", content=f"previous_summary: {resp.content}")
        except Exception:  # noqa: BLE001
            # 摘要失败降级：直接丢弃中间，不阻塞主流程
            logger.warning("context compression failed, falling back to truncation")
            return head + tail
        return head + [summary_msg] + tail


@dataclass(slots=True)
class _SimpleToolDef:
    """轻量 ToolDef 替身，仅满足 ToolExecutor.execute 签名（name 字段）。"""

    name: str


class AgentDelegateExecutor:
    """P3-12：multi-agent A2A 工具执行器。

    把另一个 Agent 注册为可调用工具（tool type = ``agent_delegate``）。
    执行时按 tool name 查找目标 agent_id，调用 ``agent_runner`` 跑目标 Agent，
    返回其 ``final_answer`` 作为观察结果。

    可组合：传入 ``inner`` ToolExecutor 处理非 delegate 工具，单实例即可服务
    混合工具集（一个 Agent 同时有 search/calc 与 agent_delegate 工具）。

    ``agent_runner`` 签名：``async (agent_id: uuid.UUID, input: str) -> str``，
    由 service 层注入（负责加载目标 Agent + 构造 LLMClient + 跑 AgentExecutor）。
    """

    def __init__(
        self,
        agent_tools: list[dict[str, Any]],
        agent_runner: Any,
        inner: ToolExecutor | None = None,
    ) -> None:
        # name → tool config（含 agent_id），仅登记 agent_delegate 类型工具
        self._delegate_map: dict[str, str] = {}
        for t in agent_tools:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type", "")
            if ttype == "agent_delegate":
                name = t.get("name", "")
                agent_id = t.get("config", {}).get("agent_id", "")
                if name and agent_id:
                    self._delegate_map[name] = str(agent_id)
        self._runner = agent_runner
        self._inner = inner

    def can_handle(self, tool_type: ToolType) -> bool:
        """是否可处理该工具类型。"""
        if tool_type == ToolType.AGENT_DELEGATE:
            return True
        return self._inner.can_handle(tool_type) if self._inner else False

    async def execute(self, tool: Any, args: dict[str, Any]) -> str:
        """执行工具调用。agent_delegate 走 A2A 委托，其余转交 inner。"""
        tool_name = getattr(tool, "name", "")
        target_agent_id = self._delegate_map.get(tool_name)
        if target_agent_id is not None:
            # P3-12：A2A 委托 — 把 input 传给目标 Agent
            delegate_input = str(args.get("input", args.get("query", "")))
            try:
                result: str = await self._runner(
                    uuid.UUID(target_agent_id), delegate_input
                )
                return result
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "agent delegate %s -> %s 执行失败: %s",
                    tool_name, target_agent_id, exc,
                )
                return f"[{tool_name} 委托失败] {exc}"
        if self._inner is not None:
            return await self._inner.execute(tool, args)
        return f"[{tool_name} 无可用执行器]"


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
