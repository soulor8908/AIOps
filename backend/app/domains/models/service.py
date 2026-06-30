"""Model Router — 业务逻辑纯函数。

含 fallback 逻辑：primary 失败按 priority 升序尝试备选模型。
路由策略：direct / round_robin / least_cost / latency。
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import LLMError, NotFoundError
from app.core.llm_client import LLMClient, LLMConfig, Message
from app.domains.models.models import (
    ChatRequest,
    ChatResponse,
    ModelConfig,
    ModelConfigCreate,
    ModelConfigUpdate,
    RoutingStrategy,
)


async def create_model(session: AsyncSession, payload: ModelConfigCreate) -> ModelConfig:
    """创建模型配置。alias 唯一冲突由 DB 约束保证。"""
    config = ModelConfig(
        alias=payload.alias,
        provider=payload.provider.value,
        model_name=payload.model_name,
        api_base=payload.api_base,
        api_key_env=payload.api_key_env,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        cost_per_1k_input=payload.cost_per_1k_input,
        cost_per_1k_output=payload.cost_per_1k_output,
        is_active=payload.is_active,
        priority=payload.priority,
    )
    session.add(config)
    await session.flush()
    return config


async def get_model(session: AsyncSession, alias: str) -> ModelConfig:
    """按 alias 获取模型配置。"""
    stmt = select(ModelConfig).where(ModelConfig.alias == alias)
    config = (await session.execute(stmt)).scalar_one_or_none()
    if config is None:
        raise NotFoundError(f"模型配置 {alias} 不存在")
    return config


async def list_models(
    session: AsyncSession, active_only: bool = False, limit: int = 100, offset: int = 0
) -> list[ModelConfig]:
    """列出模型配置，按 priority 升序。"""
    stmt = select(ModelConfig).order_by(ModelConfig.priority.asc())
    if active_only:
        stmt = stmt.where(ModelConfig.is_active.is_(True))
    stmt = stmt.limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def update_model(
    session: AsyncSession, alias: str, payload: ModelConfigUpdate
) -> ModelConfig:
    """更新模型配置。"""
    config = await get_model(session, alias)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(config, key, value)
    await session.flush()
    return config


async def delete_model(session: AsyncSession, alias: str) -> None:
    """删除模型配置。"""
    config = await get_model(session, alias)
    await session.delete(config)
    await session.flush()


async def route_model(
    session: AsyncSession, alias: str, strategy: RoutingStrategy
) -> list[ModelConfig]:
    """按策略返回候选模型列表（含 primary + fallback）。"""
    primary = await get_model(session, alias)
    if strategy == RoutingStrategy.DIRECT:
        return [primary]
    stmt = (
        select(ModelConfig)
        .where(ModelConfig.is_active.is_(True))
        .order_by(ModelConfig.priority.asc())
    )
    candidates = list((await session.execute(stmt)).scalars().all())
    if strategy == RoutingStrategy.LEAST_COST:
        candidates.sort(key=lambda c: (c.cost_per_1k_input + c.cost_per_1k_output))
    elif strategy == RoutingStrategy.LATENCY:
        # 简化：priority 越小视为延迟越低
        candidates.sort(key=lambda c: c.priority)
    # primary 排首位
    if primary in candidates:
        candidates.remove(primary)
    return [primary, *candidates]


async def chat_completion(
    session: AsyncSession, alias: str, request: ChatRequest
) -> ChatResponse:
    """聊天补全，含 fallback。primary 失败按候选列表降级。"""
    candidates = await route_model(session, alias, request.strategy)
    if not candidates:
        raise LLMError("无可用模型")
    messages = [Message(role=m.role, content=m.content) for m in request.messages]
    last_error: Exception | None = None
    for idx, config in enumerate(candidates):
        llm_config = _to_llm_config(config, request)
        client = LLMClient(llm_config)
        try:
            resp = await client.chat(messages)
            cost = _compute_cost(config, resp.usage)
            return ChatResponse(
                content=resp.content,
                model=config.model_name,
                alias=config.alias,
                usage=resp.usage,
                cost=cost,
                fallback_used=idx > 0,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        finally:
            await client.close()
    raise LLMError(f"所有候选模型均失败: {last_error}")


def _to_llm_config(config: ModelConfig, request: ChatRequest) -> LLMConfig:
    """ORM → LLMConfig。api_key 从环境变量读取（api_key_env 指定变量名）。"""
    api_key = ""
    if config.api_key_env:
        api_key = os.environ.get(config.api_key_env, "")
    elif config.provider == "openai":
        api_key = settings.openai_api_key
    elif config.provider == "anthropic":
        api_key = settings.anthropic_api_key
    return LLMConfig(
        provider=config.provider,  # type: ignore[arg-type]
        model=config.model_name,
        api_key=api_key,
        base_url=config.api_base or "",
        temperature=request.temperature or config.temperature,
        max_tokens=request.max_tokens or config.max_tokens,
    )


def _compute_cost(config: ModelConfig, usage: dict[str, Any]) -> Decimal:
    """根据 token 用量计算成本。"""
    in_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    out_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    cost = (
        Decimal(in_tokens) / Decimal(1000) * config.cost_per_1k_input
        + Decimal(out_tokens) / Decimal(1000) * config.cost_per_1k_output
    )
    return cost.quantize(Decimal("0.000001"))


__all__ = [
    "chat_completion",
    "create_model",
    "delete_model",
    "get_model",
    "list_models",
    "route_model",
    "update_model",
]
