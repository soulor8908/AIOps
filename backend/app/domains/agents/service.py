"""Agent Orchestrator — 业务逻辑纯函数。"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.llm_client import LLMClient, LLMConfig, Provider
from app.domains.agents.executor import AgentDelegateExecutor, AgentExecutor, execute_workflow_dag
from app.domains.agents.models import (
    Agent,
    AgentCreate,
    ExecuteRequest,
    ExecutionResult,
    Workflow,
    WorkflowDef,
)
from app.domains.models.models import ModelConfig

logger = logging.getLogger("app.agents.service")

MAX_NODES = 50
MAX_TURNS = 10
# P3-12：A2A 委托最大递归深度，防止 Agent A → B → A 循环调用耗尽栈/预算
_MAX_DELEGATE_DEPTH = 3

# ModelConfig.provider 值 → LLMClient 支持的 Provider（Literal["openai","anthropic","local"]）。
# Azure OpenAI 与 custom 兼容 OpenAI 协议，映射到 "openai"。
_PROVIDER_MAP: dict[str, Provider] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "local": "local",
    "azure_openai": "openai",
    "custom": "openai",
}


async def create_agent(session: AsyncSession, payload: AgentCreate) -> Agent:
    """创建 Agent。"""
    agent = Agent(
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model_alias=payload.model_alias,
        tools=[t.model_dump() for t in payload.tools],
        max_turns=min(payload.max_turns, MAX_TURNS),
        temperature=payload.temperature,
        # P3-11：自主运维开关透传
        self_eval=payload.self_eval,
        self_heal=payload.self_heal,
        self_eval_threshold=payload.self_eval_threshold,
        self_heal_max_retries=payload.self_heal_max_retries,
    )
    session.add(agent)
    await session.flush()
    return agent


async def get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    """获取 Agent。"""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} 不存在")
    return agent


async def list_agents(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[Agent]:
    """列出 Agent。"""
    stmt = (
        select(Agent)
        .order_by(Agent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def execute_agent(
    session: AsyncSession, agent_id: uuid.UUID, request: ExecuteRequest
) -> ExecutionResult:
    """执行单个 Agent。

    事务边界：读取 agent + 查询模型配置后立即 commit 释放 DB 连接，
    LLM 调用在事务外执行（避免长事务占连接池）。``expire_on_commit=False``
    确保 agent 对象在 commit 后仍可访问。

    P3-12：若 Agent 配置了 ``agent_delegate`` 工具，构造 ``AgentDelegateExecutor``
    实现 A2A 消息传递 —— 执行时把 input 传给目标 Agent，返回其 final_answer。
    委托递归深度受限（``_MAX_DELEGATE_DEPTH``）防止 A→B→A 循环。
    """
    agent = await get_agent(session, agent_id)
    config = await _build_llm_config(session, agent.model_alias, agent.temperature)
    has_delegate = any(
        isinstance(t, dict) and t.get("type") == "agent_delegate"
        for t in (agent.tools or [])
    )
    # P3-12：委托工具需要按 agent_id 加载目标 Agent，预取所有 delegate 目标
    # 与其 model_config，在 commit 前完成 DB 读取。
    delegate_agents: dict[uuid.UUID, Agent] = {}
    delegate_configs: dict[uuid.UUID, LLMConfig] = {}
    if has_delegate:
        delegate_agents, delegate_configs = await _preload_delegate_targets(
            session, agent
        )
    # P1-3：提交事务释放 DB 连接，LLM 调用不在事务内。
    await session.commit()
    client = LLMClient(config)
    tool_executor = None
    if has_delegate:
        # A2A runner 闭包：加载目标 Agent → 构造 LLMClient → 跑 AgentExecutor
        async def _delegate_runner(
            target_id: uuid.UUID, delegate_input: str, depth: int = 0
        ) -> str:
            if depth >= _MAX_DELEGATE_DEPTH:
                return f"[委托深度超 {_MAX_DELEGATE_DEPTH}，拒绝递归以防循环]"
            target = delegate_agents.get(target_id)
            if target is None:
                return f"[目标 Agent {target_id} 不存在]"
            target_config = delegate_configs.get(target_id)
            if target_config is None:
                return f"[目标 Agent {target_id} 模型配置缺失]"
            target_client = LLMClient(target_config)
            target_executor = AgentExecutor(target_client)
            try:
                result = await target_executor.run(target, delegate_input)
                return result.final_answer
            finally:
                await target_client.close()

        tool_executor = AgentDelegateExecutor(
            agent_tools=agent.tools,
            agent_runner=_delegate_runner,
        )
    executor = AgentExecutor(client, tool_executor=tool_executor)
    try:
        return await executor.run(
            agent, request.input, max_turns=request.max_turns, context=request.context
        )
    finally:
        await client.close()


async def stream_agent(
    session: AsyncSession, agent_id: uuid.UUID, request: ExecuteRequest
) -> AsyncIterator[str]:
    """P2-8：流式执行 Agent，yield SSE 事件。

    与 ``execute_agent`` 的差异：最终答案轮逐 token 流式输出，前端可即时
    渲染打字机效果。事务边界同 ``execute_agent``（commit 释放 DB 连接后流式）。

    SSE 事件格式见 ``AgentExecutor.run_stream`` 的 yield 事件类型。
    """
    import json as _json

    agent = await get_agent(session, agent_id)
    config = await _build_llm_config(session, agent.model_alias, agent.temperature)
    await session.commit()
    client = LLMClient(config)
    executor = AgentExecutor(client)
    try:
        async for event in executor.run_stream(
            agent, request.input, max_turns=request.max_turns, context=request.context
        ):
            # ExecutionResult 不可直接 json 序列化，转 dict
            if event["type"] == "done":
                result = event["result"]
                payload = {
                    "type": "done",
                    "result": {
                        "agent_id": str(result.agent_id) if result.agent_id else None,
                        "final_answer": result.final_answer,
                        "total_tokens": result.total_tokens,
                        "success": result.success,
                        "traces": [t.model_dump(mode="json") for t in result.traces],
                    },
                }
            else:
                payload = event
            yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
    except Exception as exc:  # noqa: BLE001
        yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
    finally:
        await client.close()
    yield "data: [DONE]\n\n"


async def _preload_delegate_targets(
    session: AsyncSession, agent: Agent
) -> tuple[dict[uuid.UUID, Agent], dict[uuid.UUID, LLMConfig]]:
    """P3-12：预取所有 agent_delegate 工具引用的目标 Agent 及其 LLMConfig。

    在 commit 前批量查询，避免委托执行时每路 DB 往返。返回两个 dict 按 agent_id 索引。
    """
    target_ids: set[uuid.UUID] = set()
    for t in agent.tools or []:
        if not isinstance(t, dict) or t.get("type") != "agent_delegate":
            continue
        aid = t.get("config", {}).get("agent_id")
        if aid:
            try:
                target_ids.add(uuid.UUID(str(aid)))
            except (ValueError, TypeError):
                logger.warning("agent_delegate 工具 agent_id 无效: %s", aid)
    if not target_ids:
        return {}, {}
    targets = {
        a.id: a
        for a in (
            await session.execute(select(Agent).where(Agent.id.in_(target_ids)))
        ).scalars().all()
    }
    configs: dict[uuid.UUID, LLMConfig] = {}
    for tid, tgt in targets.items():
        try:
            configs[tid] = await _build_llm_config(
                session, tgt.model_alias, tgt.temperature
            )
        except Exception:  # noqa: BLE001
            logger.warning("委托目标 %s 模型配置加载失败", tid)
    return targets, configs


async def create_workflow(session: AsyncSession, payload: WorkflowDef) -> Workflow:
    """创建工作流。节点数超 50 抛 ValidationError（业务校验）。"""
    if len(payload.nodes) > MAX_NODES:
        raise ValidationError(f"DAG 节点数超 {MAX_NODES} 上限")
    wf = Workflow(
        name=payload.name,
        description=payload.description,
        nodes=[n.model_dump() for n in payload.nodes],
        edges=[e.model_dump() for e in payload.edges],
    )
    session.add(wf)
    await session.flush()
    return wf


async def list_workflows(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[Workflow]:
    """列出工作流。"""
    stmt = select(Workflow).order_by(Workflow.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def execute_workflow(
    session: AsyncSession, workflow_id: uuid.UUID, request: ExecuteRequest
) -> ExecutionResult:
    """执行工作流 DAG。按节点顺序逐个跑 Agent，传递上下文。

    P3：预取所有节点引用的 agent + model_config，避免 DAG 执行时每节点
    2 次 DB 查询（``get_agent`` + ``_build_llm_config``）的 N+1 问题。
    50 节点工作流：原 100 次串行 DB 往返 → 2 次批量查询。
    """
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise NotFoundError(f"Workflow {workflow_id} 不存在")

    # 预取所有节点引用的 agent（去重后批量查）
    agent_ids = {
        uuid.UUID(str(node["agent_id"]))
        for node in wf.nodes
        if node.get("agent_id")
    }
    agents_cache: dict[uuid.UUID, Agent] = {}
    mcs_cache: dict[str, ModelConfig] = {}
    if agent_ids:
        agents_cache = {
            a.id: a
            for a in (
                await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))
            ).scalars().all()
        }
        # 预取所有 model_config（按 alias 去重，priority 升序取首个）
        aliases = {a.model_alias for a in agents_cache.values()}
        if aliases:
            mc_rows = (
                await session.execute(
                    select(ModelConfig)
                    .where(
                        ModelConfig.alias.in_(aliases),
                        ModelConfig.is_active.is_(True),
                    )
                    .order_by(ModelConfig.alias, ModelConfig.priority)
                )
            ).scalars().all()
            seen: set[str] = set()
            for mc in mc_rows:
                if mc.alias not in seen:
                    mcs_cache[mc.alias] = mc
                    seen.add(mc.alias)

    # 提交事务释放 DB 连接（与 execute_agent 一致，LLM 调用在事务外）
    await session.commit()

    async def _run_node(node: dict[str, Any], node_input: str) -> ExecutionResult:
        agent_id_val = node.get("agent_id")
        if agent_id_val is None:
            return ExecutionResult(
                workflow_id=workflow_id, final_answer=node_input, success=True
            )
        aid = uuid.UUID(str(agent_id_val))
        agent = agents_cache.get(aid)
        if agent is None:
            raise NotFoundError(f"Agent {aid} 不存在")
        mc = mcs_cache.get(agent.model_alias)
        config = (
            _llm_config_from_row(mc, agent.temperature)
            if mc is not None
            else _fallback_llm_config(agent.model_alias, agent.temperature)
        )
        client = LLMClient(config)
        executor = AgentExecutor(client)
        try:
            return await executor.run(
                agent,
                node_input,
                max_turns=request.max_turns,
                context=request.context,
            )
        finally:
            await client.close()

    return await execute_workflow_dag(
        workflow_id, wf.nodes, wf.edges, _run_node, request.input
    )


async def _build_llm_config(
    session: AsyncSession, model_alias: str, temperature: float | None = None
) -> LLMConfig:
    """根据 model_alias 查询 ``model_configs`` 表构造 LLMConfig。

    以 ``model_configs`` 表为单一真源（P1-2：消除 agents 与 models 域的并行路由）：
    - 按 alias + is_active 查询，priority 升序取首个
    - 透传 provider / model_name / api_base / max_tokens / cost_per_1k_*
    - ``api_key_env`` → ``os.environ[api_key_env]`` 解析（支持 K8s Secret 注入）
    - agent.temperature 覆盖 model_config.temperature（agent 配置优先）
    - 未找到时回退到 settings 默认值

    cost_per_1k_* 透传给 LLMConfig，供 llm_client 计算 llm_cost 指标
    （observability.spec.md§5.1）。
    """
    stmt = (
        select(ModelConfig)
        .where(
            ModelConfig.alias == model_alias,
            ModelConfig.is_active.is_(True),
        )
        .order_by(ModelConfig.priority)
        .limit(1)
    )
    mc = (await session.execute(stmt)).scalar_one_or_none()

    if mc is None:
        return _fallback_llm_config(model_alias, temperature)
    return _llm_config_from_row(mc, temperature)


def _llm_config_from_row(mc: ModelConfig, temperature: float | None) -> LLMConfig:
    """从 ModelConfig ORM 行构造 LLMConfig（纯函数，无 DB 查询）。

    供 ``_build_llm_config``（单 agent 执行）与 ``execute_workflow``（DAG 批量预取）
    共享，确保两条路径的 LLMConfig 构造逻辑一致。
    """
    # 解析 API key：优先从 env var 读取，回退到 settings
    api_key = ""
    if mc.api_key_env:
        api_key = os.environ.get(mc.api_key_env, "")
        if not api_key:
            logger.warning(
                "model_config alias=%s 配置 api_key_env=%s 但环境变量未注入，"
                "LLM 调用将返回 401",
                mc.alias,
                mc.api_key_env,
            )
    elif mc.provider == "openai":
        api_key = settings.openai_api_key
    elif mc.provider == "anthropic":
        api_key = settings.anthropic_api_key

    provider = _PROVIDER_MAP.get(mc.provider, "openai")
    # agent.temperature 覆盖 model_config.temperature（None 时用 model_config 值）
    temp = temperature if temperature is not None else mc.temperature

    return LLMConfig(
        provider=provider,
        model=mc.model_name,
        api_key=api_key,
        base_url=mc.api_base or "",
        temperature=temp,
        max_tokens=mc.max_tokens,
        # Decimal → float 透传（observability.spec.md§5.1 llm_cost 计算）
        cost_per_1k_input=float(mc.cost_per_1k_input),
        cost_per_1k_output=float(mc.cost_per_1k_output),
    )


def _fallback_llm_config(model_alias: str, temperature: float | None) -> LLMConfig:
    """model_config 未找到时回退到 settings 默认值。"""
    logger.warning(
        "model_config alias=%s not found or inactive; falling back to defaults",
        model_alias,
    )
    return LLMConfig(
        provider="openai",
        model=settings.default_llm_model,
        api_key=settings.openai_api_key,
        temperature=temperature if temperature is not None else 0.7,
    )


__all__ = [
    "MAX_NODES",
    "MAX_TURNS",
    "create_agent",
    "create_workflow",
    "execute_agent",
    "execute_workflow",
    "get_agent",
    "list_agents",
    "list_workflows",
]
