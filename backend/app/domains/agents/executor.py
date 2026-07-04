"""Agent 执行器 — ReAct 循环实现（P0-2 原生 function calling）。

约 130 行。观察 → 思考 → 行动 循环，max_turns 截断。

P0-2 之前靠解析 ```tool_calls``` 文本块（脆弱，LLM 多一个空格就崩）。
P0-2 起用原生 function calling：OpenAI tools / Anthropic tool_use API，
工具调用由 provider 结构化返回，executor 直接拿到 ToolCall 列表。

P2-8：多工具并发执行（asyncio.gather）+ context 压缩（达阈值摘要）。
P1-4：注入记忆后端（``MemoryBackend``），执行前检索相关历史注入 context，
每轮结束后持久化 observation / final_answer，替换纯 LLM 摘要压缩。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.core.exceptions import ValidationError
from app.core.config import settings
from app.core.llm_client import (
    LLMClient,
    LLMResponse,
    Message,
    ToolCall,
    ToolDef,
)
from app.core.metrics import metrics
from app.domains.agents.memory import MemoryBackend
from app.domains.agents.models import (
    Agent,
    ExecutionResult,
    ExecutionTrace,
    ToolType,
)
from app.domains.agents.planning import Plan, Planner, Reflector
from app.domains.agents.self_diagnose import SelfDiagnoser

logger = logging.getLogger("app.agents.executor")


def _estimate_tokens(messages: list[Message]) -> int:
    """B4：粗略估算 messages 的 token 总数。

    不引入 tiktoken 依赖（多 provider 模型各异），用 ``len(text) / 4`` 近似
    （OpenAI 英文经验值，中文偏保守——实际 1 汉字约 1-2 token，此处高估
    更早触发压缩，对长任务更安全）。每条消息加 4 token overhead 模拟
    chat 格式开销（role 标记 + 分隔符）。
    """
    total = 0
    for m in messages:
        content = m.content or ""
        total += len(content) // 4 + 4
    return total


def _record_failure_safely(message: str, metadata: dict[str, Any]) -> None:
    """P2-8：向 FailureClusterer 异步记录失败（fire-and-forget，不阻塞主流程）。

    仅当 ``settings.agent_failure_clustering_enabled`` 为真时记录。
    用 ``asyncio.create_task`` 避免 await 阻塞；失败仅记日志。
    """
    from app.core.config import settings

    if not settings.agent_failure_clustering_enabled:
        return
    try:
        from app.core.failure_cluster import get_failure_clusterer

        clusterer = get_failure_clusterer()
        asyncio.create_task(clusterer.add(message, metadata))
    except Exception:  # noqa: BLE001
        logger.debug("P2-8 failure record skipped", exc_info=True)


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

    A2：``code`` 类型工具默认被拒绝（schema 不注入），仅当
    ``settings.agent_code_tool_enabled=True`` 时透传。``code`` 工具暴露给 LLM
    生成任意代码并通过 tool_call 触发执行——是脚枪，必须显式 opt-in 且
    由调用方注入沙箱化 ``tool_executor``。
    """
    from app.core.config import settings

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
        # A2：默认拒绝 code 工具，避免 LLM 生成任意代码触发执行
        if ttype == "code" and not settings.agent_code_tool_enabled:
            logger.warning(
                "A2 code 工具 %s 被拒绝（AGENT_CODE_TOOL_ENABLED=False），"
                "如需启用请显式配置并注入沙箱化 tool_executor",
                name,
            )
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
        memory: MemoryBackend | None = None,
        planner: Planner | None = None,
        reflector: Reflector | None = None,
    ) -> None:
        self.llm = llm_client
        self.tools = tool_executor
        # P1-4：记忆后端。None 时退化为无记忆（与 P1-4 前行为一致）。
        self.memory = memory
        # P2-10：planner/reflector。None 时退化为无 plan / 无 reflection。
        # 两者独立可单独启用，plan 失败不影响 reflect，反之亦然。
        self.planner = planner
        self.reflector = reflector

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
        # P1-4：检索相关历史记忆，注入为 system 消息。memory 为 None 或检索
        # 失败时返回 []，退化为无记忆（与 P1-4 前行为一致）。
        session_id = uuid.uuid4()
        await self._inject_retrieved_history(messages, agent.id, user_input)
        # P2-10：执行前 plan。planner 为 None 或 LLM 失败时返回 None，
        # 退化为无 plan 的 ReAct 行为。plan 注入为 system 消息引导 ReAct 循环。
        plan_obj: Plan | None = None
        if self.planner is not None:
            plan_obj = await self.planner.plan(
                user_input, tools=agent.tools, system_prompt=agent.system_prompt
            )
            if plan_obj is not None:
                messages.append(
                    Message(role="system", content=plan_obj.to_prompt())
                )
        traces: list[ExecutionTrace] = []
        final_answer = ""
        # success 仅在 LLM 给出最终答案时为 True；达到 max_turns 截断视为未完成。
        # P2-8：截断不再硬判失败，标记 success=False 但 final_answer 保留最后输出。
        success = False
        for turn in range(1, turns + 1):
            # B4：context 压缩（按 token 数）。超 ``agent_context_compress_tokens``
            # 时用 LLM 摘要历史，避免长任务超 context window。压缩事件记入 traces。
            if _estimate_tokens(messages) > settings.agent_context_compress_tokens:
                messages = await self._compress_context(
                    messages, turn=turn, traces=traces
                )
            done, answer = await self._run_turn(
                turn, messages, llm_tools, traces
            )
            # P1-4：持久化本轮 content 到记忆（observation 或 final_answer）
            await self._persist_turn_memory(
                agent.id, session_id, turn, traces[-1]
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
        # P2-10：执行后 reflection。reflector 为 None 或 LLM 失败时返回 None，
        # 不阻塞主流程。reflect 在 self-heal 之后执行，对最终答案（含 healed）
        # 进行反思，确保 reflection 评估的是真正返回给用户的答案。
        reflection_str: str | None = None
        if self.reflector is not None:
            reflection_obj = await self.reflector.reflect(
                user_input, plan_obj, traces, final_answer
            )
            if reflection_obj is not None:
                reflection_str = reflection_obj.to_summary()
        return ExecutionResult(
            agent_id=agent.id,
            final_answer=final_answer,
            traces=traces,
            total_tokens=total_tokens,
            success=success,
            eval_score=eval_score,
            eval_reason=eval_reason,
            heal_attempts=heal_attempts,
            plan=plan_obj.to_prompt() if plan_obj is not None else None,
            reflection=reflection_str,
        )

    async def run_stream(
        self,
        agent: Agent,
        user_input: str,
        max_turns: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """P6e 真 streaming：单流执行 Agent，逐 token yield SSE 事件。

        与旧实现的差异（P6e）：不再"阻塞 chat 判断工具 + 重跑 stream 输出文本"，
        改用 ``stream_chat_events`` 单流实时分流 text / tool_call 增量，
        **每轮只调一次 LLM**，砍掉最终答案轮的双倍成本。

        yield 事件类型：
        - ``{"type": "token", "content": "..."}`` — 文本 delta（含思考与最终答案）
        - ``{"type": "tool", "name": "...", "args": {...}}`` — 工具调用通知（完整 args）
        - ``{"type": "observation", "content": "..."}`` — 工具执行结果
        - ``{"type": "done", "result": ExecutionResult}`` — 执行结束（含 traces/tokens）

        self-eval/self-heal 在流式模式下不执行（重跑会重复输出 token 造成
        前端混乱），需自评的 Agent 应使用阻塞 ``run()``。
        """
        turns = min(max_turns or agent.max_turns, agent.max_turns)
        llm_tools = _agent_tools_to_llm_tools(agent.tools)
        messages = self._init_messages(agent, user_input, context)
        # P1-4：流式模式同样检索历史注入，但不做 per-turn upsert（流式 loop
        # 结构不同，per-turn 持久化会复杂化事件流）。最终答案在 done 事件前
        # 持久化一次。
        session_id = uuid.uuid4()
        await self._inject_retrieved_history(messages, agent.id, user_input)
        # P2-10：流式模式同样支持 plan 注入（不影响事件流结构）。
        # reflection 不执行（与 self-eval 同模式：流式不重跑/不二次调 LLM）。
        plan_obj: Plan | None = None
        if self.planner is not None:
            plan_obj = await self.planner.plan(
                user_input, tools=agent.tools, system_prompt=agent.system_prompt
            )
            if plan_obj is not None:
                messages.append(
                    Message(role="system", content=plan_obj.to_prompt())
                )
        traces: list[ExecutionTrace] = []
        final_answer = ""
        success = False

        for turn in range(1, turns + 1):
            # B4：context 压缩（按 token 数）。流式模式同样记入 traces。
            if _estimate_tokens(messages) > settings.agent_context_compress_tokens:
                messages = await self._compress_context(
                    messages, turn=turn, traces=traces
                )
            prev_tokens = traces[-1].tokens if traces else 0

            # P6e：单流消费 stream_chat_events，实时分流 text / tool_call / finish
            text_buf: list[str] = []
            tool_calls: list[ToolCall] = []
            usage: dict[str, int] = {}
            try:
                async for event in self.llm.stream_chat_events(
                    messages, tools=llm_tools or None
                ):
                    if event.type == "text":
                        text_buf.append(event.content)
                        yield {"type": "token", "content": event.content}
                    elif event.type == "tool_call":
                        # 增量累积，不 yield（需完整 args 才能执行；finish 时统一拿到）
                        pass
                    elif event.type == "finish":
                        # finish.content 是 text delta 的完整拼接，已逐 token yield 过，
                        # 此处仅取 tool_calls / usage，不重复 append content。
                        tool_calls = event.tool_calls
                        usage = event.usage
            except Exception:  # noqa: BLE001
                # P6e：stream 失败降级——已收集的 text 作为最终答案，避免无 done 事件
                logger.warning("stream_chat_events 失败，使用已收集文本降级")
                final_answer = "".join(text_buf)
                traces.append(ExecutionTrace(
                    turn=turn, thought=final_answer, tokens=prev_tokens
                ))
                success = bool(final_answer)
                break

            full_text = "".join(text_buf)
            total_tokens = prev_tokens + int(
                usage.get("total_tokens")
                or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            )

            if not tool_calls:
                # 最终答案轮：text 已逐 token yield，记 trace 后结束
                final_answer = full_text
                traces.append(ExecutionTrace(
                    turn=turn, thought=final_answer, tokens=total_tokens
                ))
                success = True
                break

            # 工具调用轮：yield 完整工具调用 + 执行 + 观察事件
            for tc in tool_calls:
                yield {"type": "tool", "name": tc.name, "args": tc.args}
            observation = await self._execute_tools(tool_calls)
            yield {"type": "observation", "content": observation}
            traces.append(ExecutionTrace(
                turn=turn,
                thought=full_text,
                action=json.dumps(
                    [{"name": c.name, "args": c.args} for c in tool_calls],
                    ensure_ascii=False,
                ),
                observation=observation,
                tokens=total_tokens,
            ))
            messages.append(Message(role="assistant", content=full_text))
            messages.append(Message(role="tool", content=observation))
        else:
            final_answer = (
                messages[-1].content if messages else "达到最大轮次未给出最终答案。"
            )

        total_tokens = traces[-1].tokens if traces else 0
        # P1-4：流式模式在 done 事件前持久化最终答案到记忆
        if self.memory is not None and final_answer and traces:
            await self.memory.upsert(
                agent_id=agent.id,
                session_id=session_id,
                turn=len(traces),
                content=final_answer,
                metadata={"type": "final_answer", "turn": len(traces)},
            )
        result = ExecutionResult(
            agent_id=agent.id,
            final_answer=final_answer,
            traces=traces,
            total_tokens=total_tokens,
            success=success,
            plan=plan_obj.to_prompt() if plan_obj is not None else None,
        )
        yield {"type": "done", "result": result}

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
        from app.core.config import settings
        from app.domains.evals.judge import judge_llm_with_sampling

        # B3：用采样版 judge 抑制 ±0.1 噪声，使阈值判定可靠。n_samples 从
        # settings 读取（默认 3），1 时退化为单次（向后兼容）。
        n_samples = settings.agent_self_eval_samples
        threshold = float(getattr(agent, "self_eval_threshold", 0.7))
        max_retries = (
            int(getattr(agent, "self_heal_max_retries", 0))
            if getattr(agent, "self_heal", False)
            else 0
        )
        current_answer = initial_answer
        heal_attempts = 0
        try:
            judge_result = await judge_llm_with_sampling(
                actual=current_answer,
                expected=user_input,
                client=self.llm,
                criteria="回答是否准确、相关且完整地回应了用户问题",
                samples=n_samples,
            )
        except Exception:  # noqa: BLE001
            logger.warning("self-eval judge 调用失败，跳过自愈合")
            return None, None, None, 0, 0
        eval_score = judge_result.score
        eval_reason = judge_result.reason
        if eval_score >= threshold or max_retries <= 0:
            return eval_score, eval_reason, None, 0, 0
        # 自愈合：追加反馈消息重跑
        # P1-7：用 self-diagnose 替换通用反馈——先根因分析再选修复策略。
        diagnoser = SelfDiagnoser()
        for _ in range(max_retries):
            heal_attempts += 1
            diagnosis = diagnoser.diagnose(eval_reason or "", current_answer)
            feedback = (
                f"上一轮回答质量自评不达标（score={eval_score:.2f}, "
                f"threshold={threshold:.2f}）。{diagnosis.feedback}"
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
                judge_result = await judge_llm_with_sampling(
                    actual=current_answer,
                    expected=user_input,
                    client=self.llm,
                    criteria="回答是否准确、相关且完整地回应了用户问题",
                    samples=n_samples,
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

    async def _inject_retrieved_history(
        self,
        messages: list[Message],
        agent_id: uuid.UUID,
        user_input: str,
    ) -> None:
        """P1-4：检索相关历史记忆，追加为 system 消息（原地修改 messages）。

        memory 为 None 或检索返回空时不追加（避免无谓 system 消息污染 prompt）。
        检索失败由 ``MemoryBackend`` 实现捕获，此处不 try/except。
        """
        if self.memory is None:
            return
        history = await self.memory.search(agent_id, user_input)
        if not history:
            return
        messages.append(
            Message(
                role="system",
                content=f"relevant_history: {json.dumps(history, ensure_ascii=False)}",
            )
        )

    async def _persist_turn_memory(
        self,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        turn: int,
        trace: ExecutionTrace,
    ) -> None:
        """P1-4：持久化单轮 content 到记忆。

        优先存 observation（工具产出的事实），无 observation 时存 thought
        （最终答案轮 thought 即 final_answer）。memory 为 None 时 no-op，
        upsert 失败由 ``MemoryBackend`` 实现捕获，不阻塞主流程。
        """
        if self.memory is None:
            return
        content = trace.observation or trace.thought
        if not content:
            return
        metadata: dict[str, Any] = {"turn": turn}
        if trace.observation:
            metadata["type"] = "observation"
        else:
            metadata["type"] = "final_answer"
        await self.memory.upsert(
            agent_id=agent_id,
            session_id=session_id,
            turn=turn,
            content=content,
            metadata=metadata,
        )

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

        P2-9：每个工具调用记录到 metrics（tool_calls + tool_errors），
        用于工具成功率和失败模式聚类。
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
            # P2-9：记录工具调用指标（无论成功失败均计 tool_calls）
            metrics.record_tool_call(tc.name)
            if isinstance(res, Exception):
                # P2-9：记录失败，error_type 为异常类名用于失败模式聚类
                metrics.record_tool_error(tc.name, type(res).__name__)
                # P2-8：向量化 error message 存入 FailureClusterer（fire-and-forget）
                _record_failure_safely(f"[{tc.name}] {res}", {"tool": tc.name})
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

    async def _compress_context(
        self,
        messages: list[Message],
        *,
        turn: int,
        traces: list[ExecutionTrace],
    ) -> list[Message]:
        """B4：context 压缩（按 token 数）。保留 system + 最近若干轮，中间用 LLM 摘要替代。

        阈值取自 ``settings.agent_context_compress_tokens``（默认 4000）。
        压缩触发时记入 traces（``thought="[context_compressed]"``，
        ``observation`` 记摘要前后 token 数 + 消息数，``tokens`` 记本次摘要调用 token），
        便于 Trace Timeline 还原压缩事件。

        摘要失败时降级为简单截断（保留首尾），仍记 trace 标注降级。
        """
        threshold = settings.agent_context_compress_tokens
        before_tokens = _estimate_tokens(messages)
        before_count = len(messages)
        # 保留首条 system + 最近若干轮，中间摘要素。
        # tail_size 自适应：消息多时保留最近 6 条，消息少时保留更少以确保
        # 至少有 1 条 middle 可压缩（B4 之前固定 tail=6 导致 ≤7 条消息无法压缩）。
        n = len(messages)
        if n < 4:
            # 消息太少（system + ≤2 条），压缩无意义，直接返回
            return messages
        tail_size = min(6, n - 2)
        head = messages[:1]
        middle = messages[1:-tail_size]
        tail = messages[-tail_size:]
        if not middle:
            return messages
        summary_text = "\n".join(f"[{m.role}] {m.content[:200]}" for m in middle)
        summary_tokens = 0
        try:
            resp = await self.llm.chat([
                Message(role="system", content="Summarize the conversation so far concisely."),
                Message(role="user", content=summary_text),
            ])
            summary_tokens = int(resp.usage.get("total_tokens", 0))
            summary_msg = Message(role="system", content=f"previous_summary: {resp.content}")
            new_messages = head + [summary_msg] + tail
        except Exception:  # noqa: BLE001
            # 摘要失败降级：直接丢弃中间，不阻塞主流程
            logger.warning("context compression failed, falling back to truncation")
            new_messages = head + tail
        after_tokens = _estimate_tokens(new_messages)
        after_count = len(new_messages)
        # B4：压缩事件记入 traces，便于 Trace Timeline 还原
        traces.append(ExecutionTrace(
            turn=turn,
            thought="[context_compressed]",
            action=None,
            observation=(
                f"messages {before_count}->{after_count}; "
                f"tokens {before_tokens}->{after_tokens} (threshold={threshold})"
            ),
            tokens=summary_tokens,
        ))
        return new_messages


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
    *,
    condition_evaluator: Any | None = None,
) -> ExecutionResult:
    """DAG 执行：按拓扑分层并发执行节点，edge.condition 决定后继是否激活。

    A3：``WorkflowEdge.condition`` 现在被实际求值。``condition`` 为 None/空时
    无条件激活；非空时调用 ``condition_evaluator(condition, prev_output) -> bool``
    判定是否激活后继。``condition_evaluator`` 由调用方注入（典型为 LLM judge
    实现），为 None 时退化为"非空 condition 一律放行"（保持向后兼容，等价于
    旧实现的"忽略 condition"）。

    B2：按拓扑分层 ``asyncio.gather`` 并发执行同层独立节点。原串行实现下
    ``A→B, A→C, B+C→D`` 三层 DAG 中 B、C 串行（延迟 = T(B)+T(C)），现在并发
    （延迟 = max(T(B), T(C))）。50 节点上限下并发开销可控。

    条件分支语义：``condition`` 求值失败时默认放行（保守策略：宁可执行不可
    漏执行）。``condition_evaluator`` 失败不应阻塞主流程。

    ``condition_evaluator`` 签名：``async (condition: str, prev_output: str) -> bool``。
    为 None 时退化为"非空 condition 一律放行"（保持向后兼容，等价于旧实现的
    "忽略 condition"）。典型实现见 ``service._build_condition_evaluator``——
    包装 LLM judge 判定前驱输出是否满足 condition 表达式。
    """
    if not nodes:
        raise ValidationError("工作流无节点")
    if len(nodes) > 50:
        raise ValidationError("DAG 节点数超 50 上限")
    node_map = {node["id"]: node for node in nodes}
    # 构造邻接表 + 反向入度（用于拓扑分层）
    adjacency: dict[str, list[tuple[str, str | None]]] = {nid: [] for nid in node_map}
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    for edge in edges:
        src = edge.get("source")
        tgt = edge.get("target")
        cond = edge.get("condition")
        if src in node_map and tgt in node_map:
            adjacency[src].append((tgt, cond))
            in_degree[tgt] += 1
    # 入口节点：is_entry 标记优先，否则取入度为 0 的首个节点（无 edges 时全入度为 0）
    entry_id = next(
        (n["id"] for n in nodes if n.get("is_entry")),
        next((nid for nid, d in in_degree.items() if d == 0), nodes[0]["id"]),
    )
    # 拓扑分层：BFS 按层组织，每层节点无依赖可并发执行
    layers = _topological_layers(entry_id, node_map, adjacency)
    context: dict[str, str] = {"__input__": entry_input}
    activated: set[str] = set()
    traces: list[ExecutionTrace] = []
    turn_counter = 0
    for layer in layers:
        # 同层节点过滤：已被前驱 condition 拒绝的节点跳过。
        # A3：_is_node_reachable 为 async（condition_evaluator 可能为 LLM 调用），
        # 用顺序 await 逐节点判定（同层节点数通常很少，无需并发判定）。
        runnable: list[str] = []
        for nid in layer:
            if nid in activated:
                continue
            if await _is_node_reachable(
                nid, adjacency, activated, context, condition_evaluator
            ):
                runnable.append(nid)
        if not runnable:
            continue
        # B2：同层独立节点并发执行（asyncio.gather），保留 traces 顺序按 node_id
        # 入参：每个节点接收上游 final_answer（多入度时取首个激活前驱的输出，
        # __input__ 兜底为 entry_input 或上一层的最终输出）
        async def _safe_run(nid: str) -> tuple[str, ExecutionResult]:
            node = node_map[nid]
            node_input = context.get("__input__", "")
            try:
                res = await agent_runner(node, node_input)
            except Exception as exc:  # noqa: BLE001
                res = ExecutionResult(
                    workflow_id=workflow_id,
                    final_answer=f"[节点 {node.get('name', nid)} 执行失败] {exc}",
                    success=False,
                    error=str(exc),
                )
            return nid, res

        results = await asyncio.gather(*(_safe_run(nid) for nid in runnable))
        # 按节点入参顺序回写 context（后写入覆盖先写入，等价于"最后激活前驱的输出"）
        for nid, res in results:
            context[nid] = res.final_answer
            context["__input__"] = res.final_answer
            activated.add(nid)
            turn_counter += 1
            traces.extend(res.traces)
            traces.append(ExecutionTrace(
                turn=turn_counter,
                thought=f"node={node_map[nid]['name']}",
                observation=res.final_answer,
                tokens=res.total_tokens,
            ))
    return ExecutionResult(
        workflow_id=workflow_id,
        final_answer=context["__input__"],
        traces=traces,
        total_tokens=sum(t.tokens for t in traces),
    )


def _topological_layers(
    entry_id: str,
    node_map: dict[str, dict[str, Any]],
    adjacency: dict[str, list[tuple[str, str | None]]],
) -> list[list[str]]:
    """按拓扑序分层，每层节点间无依赖可并发执行。

    算法：BFS 从 entry 出发，节点入度归零时入下一层。无 edges 时所有节点同层
    （保持原行为：顺序执行）。
    """
    # 重建入度（基于 adjacency）
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    for src, targets in adjacency.items():
        for tgt, _cond in targets:
            in_degree[tgt] += 1
    # entry 节点入度强制为 0（防止有外部边指向 entry 导致死锁）
    in_degree[entry_id] = 0
    layers: list[list[str]] = []
    current: list[str] = [entry_id]
    visited: set[str] = set()
    while current:
        layers.append(current)
        next_layer: list[str] = []
        for nid in current:
            visited.add(nid)
            for tgt, _cond in adjacency.get(nid, []):
                in_degree[tgt] -= 1
                if in_degree[tgt] <= 0 and tgt not in visited and tgt not in next_layer:
                    next_layer.append(tgt)
        # 防御：避免重复节点
        current = [n for n in next_layer if n not in visited]
    # 兜底：未访问的孤立节点（无路径从 entry 可达）补到最后作为独立层
    orphans = [nid for nid in node_map if nid not in visited]
    if orphans:
        layers.append(orphans)
    return layers


async def _is_node_reachable(
    nid: str,
    adjacency: dict[str, list[tuple[str, str | None]]],
    activated: set[str],
    context: dict[str, str],
    condition_evaluator: Any | None,
) -> bool:
    """检查节点是否可达：至少一个激活前驱的 edge.condition 求值为真。

    无前驱（entry 或孤立节点）：可达。``condition_evaluator`` 为 None 时
    非空 condition 一律放行（等价于旧实现的"忽略 condition"）。

    A3：``condition_evaluator`` 是 async callable（LLM judge 调用）。
    求值失败时保守放行（不漏执行）。
    """
    has_pred = False
    for src, targets in adjacency.items():
        if src not in activated:
            continue
        for tgt, cond in targets:
            if tgt != nid:
                continue
            has_pred = True
            if not cond:
                # 无 condition：无条件激活
                return True
            if condition_evaluator is None:
                # 旧模式：有 condition 但无 evaluator，放行（向后兼容）
                return True
            try:
                prev_output = context.get(src, "")
                # A3：condition_evaluator 可能是 async（LLM judge）或 sync（测试 stub）
                result = condition_evaluator(cond, prev_output)
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    return True
            except Exception:  # noqa: BLE001
                # 求值失败：保守放行（不漏执行）
                logger.warning(
                    "A3 condition 求值失败，放行后继（cond=%s）", cond, exc_info=True
                )
                return True
    # entry 节点 / 孤立节点（无前驱）：可达
    return not has_pred
