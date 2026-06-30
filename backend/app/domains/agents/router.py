"""Agent Orchestrator — FastAPI 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.domains.agents import service
from app.domains.agents.models import (
    AgentCreate,
    AgentOut,
    ExecuteRequest,
    ExecutionResult,
    WorkflowDef,
    WorkflowOut,
)

router = APIRouter(tags=["agents"])


# ===================== Agents =====================

@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[AgentOut]:
    agents = await service.list_agents(session, limit=limit, offset=offset)
    return [AgentOut.model_validate(a) for a in agents]


@router.post("/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, session: AsyncSession = Depends(get_session)
) -> AgentOut:
    agent = await service.create_agent(session, payload)
    return AgentOut.model_validate(agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> AgentOut:
    agent = await service.get_agent(session, agent_id)
    return AgentOut.model_validate(agent)


@router.post("/agents/{agent_id}/execute", response_model=ExecutionResult)
async def execute_agent(
    agent_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResult:
    return await service.execute_agent(session, agent_id, payload)


# ===================== Workflows =====================

@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[WorkflowOut]:
    wfs = await service.list_workflows(session, limit=limit, offset=offset)
    return [WorkflowOut.model_validate(w) for w in wfs]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowDef, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    wf = await service.create_workflow(session, payload)
    return WorkflowOut.model_validate(wf)


@router.post("/workflows/{workflow_id}/execute", response_model=ExecutionResult)
async def execute_workflow(
    workflow_id: uuid.UUID,
    payload: ExecuteRequest,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResult:
    return await service.execute_workflow(session, workflow_id, payload)
