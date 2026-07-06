"""Agent Orchestrator — FastAPI 路由。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.core.exceptions import GatewayTimeoutError
from app.domains.agents import service
from app.domains.agents.models import (
    AgentCreate,
    AgentOut,
    ExecuteRequest,
    ExecutionResult,
    WorkflowDef,
    WorkflowOut,
)
from app.domains.auth.models import User

router = APIRouter(tags=["agents"])


async def _with_request_timeout(coro: Any) -> Any:
    """P0-20：请求级超时包裹。

    长 LLM 调用（多轮 ReAct + 工具执行）超 ``agent_execute_timeout_seconds``
    时抛 ``GatewayTimeoutError`` (504)。客户端已等待过久，继续跑只会产生
    LLM 成本但结果丢弃。超时后协程被 cancel，service 内部的 LLM 调用
    在 cancel 传播时中止（httpx 请求在线程池中可能跑完但结果被丢弃）。
    """
    try:
        return await asyncio.wait_for(
            coro, timeout=settings.agent_execute_timeout_seconds
        )
    except TimeoutError as exc:
        raise GatewayTimeoutError(
            f"请求超 {settings.agent_execute_timeout_seconds}s 超时"
        ) from exc


# ===================== Agents =====================

@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentOut]:
    # P4-2：非 admin 仅能查看自己的 Agent
    owner_id = None if current_user.is_admin else current_user.id
    agents = await service.list_agents(session, limit=limit, offset=offset, owner_id=owner_id)
    return [AgentOut.model_validate(a) for a in agents]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> AgentOut:
    # P4-2：绑定当前 admin 为 owner
    agent = await service.create_agent(session, payload, owner_id=current_user.id)
    return AgentOut.model_validate(agent)


@router.get("/agents/failure-clusters")
async def list_failure_clusters(
    distance_threshold: float | None = Query(default=None, ge=0.0, le=2.0),
    current_admin: User = Depends(get_current_admin),
) -> list[dict[str, Any]]:
    """查看失败模式聚类（需 admin 权限）。

    返回按 count 降序的簇列表，每簇含代表消息、计数、样本。
    ``distance_threshold`` 可覆盖默认阈值（余弦距离，越小簇越细）。

    注意：此静态路径必须在 ``/agents/{agent_id}`` 之前注册，否则会被
    路径参数拦截（与 P0-3 同模式）。
    """
    from app.core.failure_cluster import get_failure_clusterer

    clusterer = get_failure_clusterer()
    clusters = clusterer.cluster(distance_threshold=distance_threshold)
    return [
        {
            "cluster_id": c.cluster_id,
            "representative_message": c.representative_message,
            "count": c.count,
            "samples": [
                {"message": s.message, "metadata": s.metadata} for s in c.samples
            ],
        }
        for c in clusters
    ]


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentOut:
    # P4-2：非 admin 校验所有权
    owner_id = None if current_user.is_admin else current_user.id
    agent = await service.get_agent(session, agent_id, owner_id=owner_id)
    return AgentOut.model_validate(agent)


@router.post("/agents/{agent_id}/execute", response_model=ExecutionResult)
async def execute_agent(
    agent_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExecutionResult:
    # P4-2：非 admin 校验所有权（执行 Agent 需拥有该 Agent）
    owner_id = None if current_user.is_admin else current_user.id
    # P0-20：请求级超时，超时抛 504 gateway_timeout
    return await _with_request_timeout(
        service.execute_agent(session, agent_id, payload, owner_id=owner_id)
    )


@router.post("/agents/{agent_id}/execute/stream")
async def execute_agent_stream(
    agent_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """P2-8：流式执行 Agent，SSE 逐 token 输出最终答案。

    事件类型：token（逐 token）/ tool（工具调用）/ observation（观察）/ done / error。
    前端用 EventSource 消费，打字机效果即时渲染。
    """
    # P4-2：非 admin 校验所有权
    owner_id = None if current_user.is_admin else current_user.id
    return StreamingResponse(
        service.stream_agent(session, agent_id, payload, owner_id=owner_id),
        media_type="text/event-stream",
    )


# ===================== Workflows =====================

@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[WorkflowOut]:
    # P4-2：非 admin 仅能查看自己的 Workflow
    owner_id = None if current_user.is_admin else current_user.id
    wfs = await service.list_workflows(session, limit=limit, offset=offset, owner_id=owner_id)
    return [WorkflowOut.model_validate(w) for w in wfs]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowDef,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> WorkflowOut:
    # P4-2：绑定当前 admin 为 owner
    wf = await service.create_workflow(session, payload, owner_id=current_user.id)
    return WorkflowOut.model_validate(wf)


@router.post("/workflows/{workflow_id}/execute", response_model=ExecutionResult)
async def execute_workflow(
    workflow_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExecutionResult:
    # P4-2：非 admin 校验所有权（执行 Workflow 需拥有该 Workflow）
    owner_id = None if current_user.is_admin else current_user.id
    # P0-20：请求级超时，超时抛 504 gateway_timeout
    return await _with_request_timeout(
        service.execute_workflow(session, workflow_id, payload, owner_id=owner_id)
    )
