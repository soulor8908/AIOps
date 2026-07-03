"""Model Router 路由策略与 fallback eval（models/SPEC.md Success Criteria）。

覆盖 8 项验收：
1. direct 策略仅返回 primary
2. round_robin 在 active 候选中轮转，primary 居首
3. least_cost 按 (input+output) 单价升序
4. latency 按 priority 升序
5. primary 失败时降级并标记 fallback_used
6. 成本计算精度到 6 位小数
7. 所有候选均失败 → LLMError（附 last_error）
8. azure_openai/custom 未配 api_base → 跳过（避免静默失败）

通过 ``LLMClient.chat`` 的 monkeypatch 控制每个候选的成功/失败，不发起真实网络请求。
直接测 service 层（route_model / chat_completion / _compute_cost），绕过 HTTP 层。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import LLMError
from app.core.llm_client import LLMResponse
from app.domains.models import service as model_service
from app.domains.models.models import (
    ChatMessage,
    ChatRequest,
    ModelConfig,
    RoutingStrategy,
)

# ===================== 辅助：构造 ModelConfig =====================


def _config(
    alias: str,
    *,
    provider: str = "openai",
    priority: int = 100,
    cost_in: str = "0.01",
    cost_out: str = "0.03",
    is_active: bool = True,
    api_base: str | None = None,
    api_key_env: str | None = None,
) -> ModelConfig:
    """构造内存 ModelConfig（不入库，供 route_model/chat_completion 的 mock 场景）。"""
    return ModelConfig(
        alias=alias,
        provider=provider,
        model_name=f"model-{alias}",
        api_base=api_base,
        api_key_env=api_key_env,
        max_tokens=4096,
        temperature=0.7,
        cost_per_1k_input=Decimal(cost_in),
        cost_per_1k_output=Decimal(cost_out),
        is_active=is_active,
        priority=priority,
    )


def _chat_request(strategy: RoutingStrategy = RoutingStrategy.DIRECT) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        strategy=strategy,
    )


def _mock_chat(content: str = "ok", usage: dict | None = None):
    """返回一个 mock chat 协程，恒定成功。"""
    from unittest.mock import AsyncMock

    return AsyncMock(
        return_value=LLMResponse(
            content=content,
            usage=usage or {"prompt_tokens": 100, "completion_tokens": 50},
        )
    )


# ===================== 1. direct 仅返回 primary =====================


@pytest.mark.asyncio
async def test_direct_strategy_returns_only_primary() -> None:
    """direct 策略仅返回 primary（SPEC：direct 仅返回 primary）。"""
    primary = _config("primary", priority=10)
    other = _config("other", priority=20)
    session = _MockSession([primary, other])

    candidates = await model_service.route_model(
        session, "primary", RoutingStrategy.DIRECT
    )
    assert candidates == [primary]


# ===================== 2. round_robin 轮转，primary 居首 =====================


@pytest.mark.asyncio
async def test_round_robin_primary_first_and_rotates() -> None:
    """round_robin：primary 始终居首，其余 active 候选轮转。"""
    # 重置 round_robin 索引避免跨测试污染
    model_service._round_robin_index.clear()

    a = _config("a", priority=10)
    b = _config("b", priority=20)
    c = _config("c", priority=30)
    session = _MockSession([a, b, c])

    # 第一次调用：primary=a 居首，其余 [b, c] 不移位
    cands1 = await model_service.route_model(
        session, "a", RoutingStrategy.ROUND_ROBIN
    )
    assert cands1[0] is a  # primary 居首
    assert set(cands1[1:]) == {b, c}

    # 第二次调用：索引推进，其余候选应轮转（b/c 顺序变化）
    cands2 = await model_service.route_model(
        session, "a", RoutingStrategy.ROUND_ROBIN
    )
    assert cands2[0] is a  # primary 仍居首
    # 轮转后尾部顺序应与第一次不同（除非仅 1 个候选）
    if len(cands1) > 2:
        assert cands1[1:] != cands2[1:]


# ===================== 3. least_cost 按单价升序 =====================


@pytest.mark.asyncio
async def test_least_cost_orders_by_total_unit_price() -> None:
    """least_cost 按 (input+output) 单价升序排列候选（primary 仍居首）。"""
    cheap = _config("cheap", priority=10, cost_in="0.001", cost_out="0.002")
    pricey = _config("pricey", priority=20, cost_in="0.05", cost_out="0.10")
    primary = _config("primary", priority=5, cost_in="0.02", cost_out="0.04")
    session = _MockSession([primary, cheap, pricey])

    candidates = await model_service.route_model(
        session, "primary", RoutingStrategy.LEAST_COST
    )
    # primary 始终居首
    assert candidates[0] is primary
    # 其余按 (input+output) 升序：cheap(0.003) < pricey(0.15)
    rest = candidates[1:]
    assert rest[0] is cheap
    assert rest[1] is pricey


# ===================== 4. latency 按 priority 升序 =====================


@pytest.mark.asyncio
async def test_latency_strategy_orders_by_priority() -> None:
    """latency 策略按 priority 升序（priority 越小视为延迟越低）。"""
    fast = _config("fast", priority=10)
    mid = _config("mid", priority=50)
    slow = _config("slow", priority=200)
    primary = _config("primary", priority=100)
    session = _MockSession([primary, fast, mid, slow])

    candidates = await model_service.route_model(
        session, "primary", RoutingStrategy.LATENCY
    )
    assert candidates[0] is primary  # primary 居首
    rest = candidates[1:]
    # 按 priority 升序：fast(10) < mid(50) < slow(200)
    assert [c.priority for c in rest] == [10, 50, 200]


# ===================== 5. primary 失败降级 + fallback_used =====================


@pytest.mark.asyncio
async def test_fallback_when_primary_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """primary 失败时降级到下一个候选，响应标记 fallback_used=True。"""
    primary = _config("primary", priority=10)
    backup = _config("backup", priority=20)
    session = _MockSession([primary, backup])

    call_log: list[str] = []

    async def _fake_chat(self, messages):  # type: ignore[no-untyped-def]
        call_log.append(self.config.model)
        if self.config.model == "model-primary":
            raise LLMError("primary down")
        return LLMResponse(
            content="from-backup",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    async def _fake_close(self) -> None:
        pass

    monkeypatch.setattr("app.domains.models.service.LLMClient.chat", _fake_chat)
    monkeypatch.setattr("app.domains.models.service.LLMClient.close", _fake_close)

    resp = await model_service.chat_completion(
        session, "primary", _chat_request(RoutingStrategy.LEAST_COST)
    )
    assert resp.content == "from-backup"
    assert resp.alias == "backup"
    assert resp.fallback_used is True
    # primary 被尝试过（失败后 continue）
    assert "model-primary" in call_log


# ===================== 6. 成本计算精度 6 位小数 =====================


def test_compute_cost_precision_six_decimals() -> None:
    """_compute_cost 量化到 6 位小数（SPEC：quantize(0.000001)）。"""
    config = _config("c", cost_in="0.01", cost_out="0.03")
    # 100 input + 50 output: 100/1000*0.01 + 50/1000*0.03 = 0.001 + 0.0015 = 0.0025
    cost = model_service._compute_cost(
        config, {"prompt_tokens": 100, "completion_tokens": 50}
    )
    assert cost == Decimal("0.002500")
    # 非整数结果保留 6 位
    assert cost.as_tuple().exponent == -6


def test_compute_cost_handles_anthropic_usage_keys() -> None:
    """_compute_cost 兼容 Anthropic 的 input_tokens/output_tokens 字段名。"""
    config = _config("c", cost_in="0.01", cost_out="0.03")
    cost = model_service._compute_cost(
        config, {"input_tokens": 100, "output_tokens": 50}
    )
    assert cost == Decimal("0.002500")


# ===================== 7. 所有候选失败 → LLMError =====================


@pytest.mark.asyncio
async def test_all_candidates_fail_raises_llm_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """所有候选均失败 → LLMError 且消息含 last_error。"""
    primary = _config("primary", priority=10)
    backup = _config("backup", priority=20)
    session = _MockSession([primary, backup])

    async def _always_fail(self, messages):  # type: ignore[no-untyped-def]
        raise LLMError("all down")

    async def _fake_close(self) -> None:
        pass

    monkeypatch.setattr("app.domains.models.service.LLMClient.chat", _always_fail)
    monkeypatch.setattr("app.domains.models.service.LLMClient.close", _fake_close)

    with pytest.raises(LLMError) as exc_info:
        await model_service.chat_completion(
            session, "primary", _chat_request(RoutingStrategy.LEAST_COST)
        )
    assert "all down" in str(exc_info.value)


# ===================== 8. azure_openai/custom 未配 api_base 跳过 =====================


@pytest.mark.asyncio
async def test_azure_without_api_base_skipped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """azure_openai 未配 api_base → 跳过该候选（避免静默失败）。"""
    # primary=azure 无 api_base（应跳过），backup=openai 正常
    primary = _config("azure-primary", provider="azure_openai", api_base=None)
    backup = _config("backup", provider="openai", priority=20)
    session = _MockSession([primary, backup])

    attempted: list[str] = []

    async def _fake_chat(self, messages):  # type: ignore[no-untyped-def]
        attempted.append(self.config.model)
        return LLMResponse(content="ok", usage={"prompt_tokens": 5, "completion_tokens": 5})

    async def _fake_close(self) -> None:
        pass

    monkeypatch.setattr("app.domains.models.service.LLMClient.chat", _fake_chat)
    monkeypatch.setattr("app.domains.models.service.LLMClient.close", _fake_close)

    resp = await model_service.chat_completion(
        session, "azure-primary", _chat_request(RoutingStrategy.LEAST_COST)
    )
    # azure 未被实际调用（因无 api_base 跳过），backup 成功
    assert resp.alias == "backup"
    assert "model-azure-primary" not in attempted
    assert resp.fallback_used is True  # 走了第 2 个候选


# ===================== Mock Session =====================


class _MockSession:
    """最小 AsyncSession mock，返回预设的 ModelConfig 列表。

    route_model 调用两次 execute：
    1. get_model → select().where(alias==x).scalar_one_or_none() → 按 alias 过滤
    2. 候选查询 → select().where(is_active).order_by(priority).scalars().all()
    本 mock 通过解析 stmt 的 whereclause 区分两种场景。
    """

    def __init__(self, configs: list[ModelConfig]) -> None:
        self._configs = configs

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        return _MockResult(self._configs, stmt)


class _MockResult:
    def __init__(self, configs: list[ModelConfig], stmt: object) -> None:
        self._configs = configs
        self._stmt = stmt

    def scalars(self):  # type: ignore[no-untyped-def]
        return self

    def all(self) -> list[ModelConfig]:
        # 候选查询：返回所有 active 配置（is_active 过滤已在 SQL 层，mock 返回全部，
        # route_model 的 LEAST_COST/LATENCY 排序在内存中处理 active 列表）
        return [c for c in self._configs if c.is_active]

    def scalar_one_or_none(self) -> ModelConfig | None:
        # get_model：按 alias 匹配（stmt.whereclause 含 alias == x）
        # 简化：返回 alias 匹配的第一个；无匹配返回 None
        stmt_str = str(self._stmt)
        for c in self._configs:
            if c.alias in stmt_str:
                return c
        return self._configs[0] if self._configs else None
