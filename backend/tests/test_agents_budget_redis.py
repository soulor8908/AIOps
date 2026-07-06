"""A1 Redis ZSET BudgetTracker 测试 — 多 pod 共享预算。

覆盖：
1. consume + remaining 基本语义（与内存版对照）
2. 滑动窗口过期淘汰
3. budget=0 不限制语义
4. is_exhausted 熔断触发
5. 多实例共享同一 Redis 视图（模拟多 pod）
6. Redis 连接失败降级（build_budget_tracker_from_settings 回退内存版）
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import fakeredis
import pytest

from app.domains.agents.budget_redis import (
    RedisBudgetTracker,
    build_budget_tracker_from_settings,
)


def _make_fake_redis():
    """返回 fakeredis 同步客户端（register_script 支持）。"""
    return fakeredis.FakeRedis()


# ===================== 1. consume + remaining =====================


def test_redis_consume_and_remaining() -> None:
    r = _make_fake_redis()
    bt = RedisBudgetTracker(budget=1000, window_seconds=60, redis_client=r)
    assert bt.remaining() == 1000
    bt.consume(300)
    assert bt.remaining() == 700
    bt.consume(200)
    assert bt.remaining() == 500


# ===================== 2. 滑动窗口过期 =====================


def test_redis_sliding_window_evicts_expired() -> None:
    r = _make_fake_redis()
    bt = RedisBudgetTracker(budget=1000, window_seconds=10, redis_client=r)
    now = 1000.0
    bt.consume(800, now=now)
    assert bt.remaining(now=now) == 200
    # 窗口外（now + 11s）应滑出
    assert bt.remaining(now=now + 11) == 1000
    assert bt.is_exhausted(now=now + 11) is False


# ===================== 3. budget=0 不限制 =====================


def test_redis_budget_zero_means_unlimited() -> None:
    r = _make_fake_redis()
    bt = RedisBudgetTracker(budget=0, window_seconds=60, redis_client=r)
    bt.consume(999999)
    assert bt.is_exhausted() is False
    assert bt.remaining() == 0  # 与内存版语义一致


# ===================== 4. is_exhausted 熔断 =====================


def test_redis_is_exhausted_when_remaining_zero() -> None:
    r = _make_fake_redis()
    bt = RedisBudgetTracker(budget=100, window_seconds=60, redis_client=r)
    assert bt.is_exhausted() is False
    bt.consume(100)
    assert bt.is_exhausted() is True


def test_redis_consume_zero_or_negative_noop() -> None:
    r = _make_fake_redis()
    bt = RedisBudgetTracker(budget=100, window_seconds=60, redis_client=r)
    bt.consume(0)
    bt.consume(-5)
    assert bt.remaining() == 100


# ===================== 5. 多实例共享视图（多 pod 模拟）=====================


def test_redis_multiple_instances_share_budget() -> None:
    """两个 RedisBudgetTracker 实例共享同一 Redis，模拟两个 pod。

    pod A consume(600) 后 pod B 视图也应反映消耗（剩余 400）。
    内存版在此场景下每实例独立计数，是 A1 修复的核心问题。
    """
    shared_redis = _make_fake_redis()
    pod_a = RedisBudgetTracker(budget=1000, window_seconds=60, redis_client=shared_redis)
    pod_b = RedisBudgetTracker(budget=1000, window_seconds=60, redis_client=shared_redis)
    pod_a.consume(600)
    # pod B 视图：1000 - 600 = 400（不是内存版的 1000）
    assert pod_b.remaining() == 400
    # pod B 继续消耗 400 应触发熔断
    pod_b.consume(400)
    assert pod_a.is_exhausted() is True
    assert pod_b.is_exhausted() is True


# ===================== 6. Redis 失败降级 =====================


def test_redis_consume_failure_does_not_raise() -> None:
    """Redis 调用失败时 consume 仅记日志，不抛异常。"""
    broken_redis = MagicMock()
    broken_redis.register_script.return_value = MagicMock(
        side_effect=ConnectionError("redis down")
    )
    bt = RedisBudgetTracker(budget=100, window_seconds=60, redis_client=broken_redis)
    # 不抛异常
    bt.consume(50)
    assert bt.is_exhausted() is True  # remaining 返回 0 → 熔断


def test_redis_remaining_failure_returns_zero_triggers_break() -> None:
    """Redis 失败时 remaining 返回 0 → is_exhausted 返回 True（保守熔断）。"""
    broken_redis = MagicMock()
    broken_redis.register_script.return_value = MagicMock(
        side_effect=ConnectionError("redis down")
    )
    bt = RedisBudgetTracker(budget=1000, window_seconds=60, redis_client=broken_redis)
    assert bt.remaining() == 0
    assert bt.is_exhausted() is True


# ===================== 7. 工厂函数 =====================


async def test_factory_default_uses_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_cost_budget_redis_enabled=False → 内存版（默认路径）。"""
    from app.domains.agents.model_router import BudgetTracker

    monkeypatch.setattr(
        "app.core.config.settings.agent_cost_budget_redis_enabled", False
    )
    tracker = await build_budget_tracker_from_settings()
    assert isinstance(tracker, BudgetTracker)


async def test_factory_redis_enabled_uses_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_cost_budget_redis_enabled=True + Redis 可达 → RedisBudgetTracker。

    用 monkeypatch 替换 redis.from_url 返回 fakeredis，模拟生产 Redis 可达。
    """
    monkeypatch.setattr(
        "app.core.config.settings.agent_cost_budget_redis_enabled", True
    )
    fake = _make_fake_redis()

    # 用 import_path 替换 sync redis.from_url
    import sys

    fake_module = MagicMock()
    fake_module.from_url = lambda *args, **kwargs: fake
    monkeypatch.setitem(sys.modules, "redis", fake_module)

    tracker = await build_budget_tracker_from_settings()
    assert isinstance(tracker, RedisBudgetTracker)


async def test_factory_redis_unreachable_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_cost_budget_redis_enabled=True + Redis 不可达 → 回退内存版（带 warning）。"""
    from app.domains.agents.model_router import BudgetTracker

    monkeypatch.setattr(
        "app.core.config.settings.agent_cost_budget_redis_enabled", True
    )
    import sys

    fake_module = MagicMock()

    def _raise(*args, **kwargs):
        raise ConnectionError("redis unreachable")

    fake_module.from_url = _raise
    monkeypatch.setitem(sys.modules, "redis", fake_module)

    tracker = await build_budget_tracker_from_settings()
    assert isinstance(tracker, BudgetTracker)
