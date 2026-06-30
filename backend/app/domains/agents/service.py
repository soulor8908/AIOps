"""Agent Orchestrator — 业务逻辑纯函数。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import LLMError, NotFoundError
from app.core.llm_client import LLMClient, LLMConfig
from app.domains.agents.executor import AgentExecutor, execute_workflow_dag
from app.domains.agents.models import (
    Agent,
    AgentCreate,
    AgentNode,
    ExecuteRequest,
    ExecutionResult,
    ToolDef,
    Workflow,
    WorkflowDef,
)

MAX_NODES = 50
MAX_TURNS = 10


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
    """执行单个 Agent。LLMClient 实例化后交给 AgentExecutor。"""
    agent = await get_agent(session, agent_id)
    config = _build_llm_config(agent.model_alias)
    client = LLMClient(config)
    executor = AgentExecutor(client)
    try:
        return await executor.run(
            agent, request.input, max_turns=request.max_turns, context=request.context
        )
    finally:
        await client.close()


async def create_workflow(session: AsyncSession, payload: WorkflowDef) -> Workflow:
    """创建工作流。节点数超 50 抛错。"""
    if len(payload.nodes) > MAX_NODES:
        raise LLMError(f"DAG 节点数超 {MAX_NODES} 上限")
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
    """执行工作流 DAG。按节点顺序逐个跑 Agent，传递上下文。"""
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise NotFoundError(f"Workflow {workflow_id} 不存在")

    async def _run_node(node: dict[str, Any], node_input: str) -> ExecutionResult:
        agent_id = node.get("agent_id")
        if agent_id is None:
            return ExecutionResult(
                workflow_id=workflow_id, final_answer=node_input, success=True
            )
        return await execute_agent(
            session, uuid.UUID(str(agent_id)), ExecuteRequest(input=node_input)
        )

    return await execute_workflow_dag(
        workflow_id, wf.nodes, wf.edges, _run_node, request.input
    )


def _build_llm_config(model_alias: str) -> LLMConfig:
    """根据 model_alias 构造 LLMConfig（极简：默认走 openai）。"""
    provider = "openai"
    model = settings.default_llm_model
    api_key = settings.openai_api_key
    alias_lower = model_alias.lower()
    if "claude" in alias_lower:
        provider = "anthropic"
        model = "claude-3-5-sonnet-20241022"
        api_key = settings.anthropic_api_key
    elif model_alias not in ("default", "gpt-4o", "gpt-4o-mini"):
        provider = "local"
        model = model_alias
    return LLMConfig(provider=provider, model=model, api_key=api_key)


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
