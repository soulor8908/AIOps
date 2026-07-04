"""Agent Orchestrator — FastAPI 路由。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
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


# ===================== Agents =====================

@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentOut]:
    agents = await service.list_agents(session, limit=limit, offset=offset)
    return [AgentOut.model_validate(a) for a in agents]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> AgentOut:
    agent = await service.create_agent(session, payload)
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
    agent = await service.get_agent(session, agent_id)
    return AgentOut.model_validate(agent)


@router.post("/agents/{agent_id}/execute", response_model=ExecutionResult)
async def execute_agent(
    agent_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExecutionResult:
    return await service.execute_agent(session, agent_id, payload)


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
    return StreamingResponse(
        service.stream_agent(session, agent_id, payload),
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
    wfs = await service.list_workflows(session, limit=limit, offset=offset)
    return [WorkflowOut.model_validate(w) for w in wfs]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowDef,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> WorkflowOut:
    wf = await service.create_workflow(session, payload)
    return WorkflowOut.model_validate(wf)


@router.post("/workflows/{workflow_id}/execute", response_model=ExecutionResult)
async def execute_workflow(
    workflow_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ExecutionResult:
    return await service.execute_workflow(session, workflow_id, payload)
