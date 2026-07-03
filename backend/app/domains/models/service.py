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
from app.core.llm_client import LLMClient, LLMConfig, Message, Provider
from app.domains.models.models import (
    ChatRequest,
    ChatResponse,
    ModelConfig,
    ModelConfigCreate,
    ModelConfigUpdate,
    RoutingStrategy,
)

_round_robin_index: dict[str, int] = {}

# ModelConfig.provider 值 → LLMClient 支持的 Provider（Literal["openai","anthropic","local"]）。
# Azure OpenAI 与 custom 兼容 OpenAI 协议，映射到 "openai"。
# 与 agents/service.py 的 _PROVIDER_MAP 保持一致。
_PROVIDER_MAP: dict[str, Provider] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "local": "local",
    "azure_openai": "openai",
    "custom": "openai",
}


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
    if strategy == RoutingStrategy.ROUND_ROBIN and candidates:
        shift = _round_robin_index.get(alias, 0) % len(candidates)
        _round_robin_index[alias] = _round_robin_index.get(alias, 0) + 1
        candidates = candidates[shift:] + candidates[:shift]
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
        if config.provider in ("azure_openai", "custom") and not config.api_base:
            last_error = LLMError(f"模型 {config.alias} 未配置 api_base")
            continue
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
        except LLMError as exc:
            # LLMClient.chat 已将所有 HTTP/JSON/结构异常统一包装为 LLMError。
            # 仅捕获 LLMError 以便降级到下一个候选模型；其它意外异常（如编程错误）
            # 应直接向上传播而非被 fallback 逻辑吞掉。
            last_error = exc
            continue
        finally:
            await client.close()
    raise LLMError(f"所有候选模型均失败: {last_error}")


def _to_llm_config(config: ModelConfig, request: ChatRequest) -> LLMConfig:
    """ORM → LLMConfig。api_key 从环境变量读取（api_key_env 指定变量名）。

    provider 经 ``_PROVIDER_MAP`` 映射到 LLMClient 支持的合法值，
    避免 azure_openai/custom 等兼容 OpenAI 协议的 provider 直接传入导致
    LLMClient 抛 "不支持的 provider"。
    """
    api_key = ""
    if config.api_key_env:
        api_key = os.environ.get(config.api_key_env, "")
    elif config.provider == "openai":
        api_key = settings.openai_api_key
    elif config.provider == "anthropic":
        api_key = settings.anthropic_api_key
    provider = _PROVIDER_MAP.get(config.provider, "openai")
    return LLMConfig(
        provider=provider,
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
