"""Redis ZSET 滑动窗口 token 预算跟踪器（A1：多副本共享预算）。

设计动机
--------
原 ``BudgetTracker``（``model_router.py``）是进程级内存实现，K8s HPA 多副本
（backend 2-6 replicas）下每个 pod 独立计数，**实际 token 消耗是 N 倍于单
pod 视图**，熔断永远不触发，成本失控。本模块用 Redis ZSET + HASH 提供跨
pod 共享视图，consume/remaining 均基于全局状态。

数据结构
--------
- ZSET ``budget:{ns}:events``：score=timestamp（秒）、member=event_uuid
- HASH ``budget:{ns}:tokens``：field=event_uuid、value=tokens（整数）

ZSET 用于按时间戳过期淘汰，HASH 用于 O(1) 取单事件 token 数。两者通过
event_uuid 关联，evict 时同步清理。

原子性
------
``consume`` 与 ``remaining`` 各用一个 Lua 脚本，保证"evict + add + sum"
在 Redis 单线程内原子执行，避免多 pod 并发写入的竞态。

失败语义
--------
- ``consume`` 失败：记 warning，不抛异常（预算跟踪不应阻塞主请求路径）
- ``remaining`` 失败：返回 0 触发熔断（保守策略：宁可误熔断不放过超支）
- ``is_exhausted`` 在 ``budget=0`` 时显式返回 False（与内存版语义一致：0 = 不限制）

降级
----
``service._get_budget_tracker`` 在 Redis 不可达时回退到内存版
``BudgetTracker``（带 warning），保证 dev/test/Redis 故障期仍可用，但失去
多 pod 共享语义——这是显式的降级路径，不应在生产长期运行。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Protocol

logger = logging.getLogger("app.agents.budget_redis")

# Lua 脚本：consume 原子执行。ARGV=[now, window, event_id, tokens, budget]
# 返回剩余预算（budget=0 时返回 0 表示"不限制"，调用方 is_exhausted 会判 False）。
_LUA_CONSUME = """
local zset_key = KEYS[1]
local hash_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local event_id = ARGV[3]
local tokens = tonumber(ARGV[4])
local budget = tonumber(ARGV[5])
local cutoff = now - window
-- 滑出过期事件（score <= cutoff）：先从 HASH 删 tokens，再从 ZSET 删 member
local expired = redis.call('ZRANGEBYSCORE', zset_key, '-inf', cutoff)
for i = 1, #expired do
    redis.call('HDEL', hash_key, expired[i])
end
if #expired > 0 then
    redis.call('ZREMRANGEBYSCORE', zset_key, '-inf', cutoff)
end
-- 添加新事件
redis.call('ZADD', zset_key, now, event_id)
redis.call('HSET', hash_key, event_id, tokens)
-- 求和
local total = 0
local all_tokens = redis.call('HVALS', hash_key)
for i = 1, #all_tokens do
    total = total + tonumber(all_tokens[i])
end
if budget == 0 then return 0 end
local remaining = budget - total
if remaining < 0 then return 0 end
return remaining
"""

# Lua 脚本：remaining 原子执行（仅淘汰过期 + 求和，不写入）。ARGV=[now, window, budget]
_LUA_REMAINING = """
local zset_key = KEYS[1]
local hash_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local budget = tonumber(ARGV[3])
local cutoff = now - window
local expired = redis.call('ZRANGEBYSCORE', zset_key, '-inf', cutoff)
for i = 1, #expired do
    redis.call('HDEL', hash_key, expired[i])
end
if #expired > 0 then
    redis.call('ZREMRANGEBYSCORE', zset_key, '-inf', cutoff)
end
if budget == 0 then return 0 end
local total = 0
local all_tokens = redis.call('HVALS', hash_key)
for i = 1, #all_tokens do
    total = total + tonumber(all_tokens[i])
end
local remaining = budget - total
if remaining < 0 then return 0 end
return remaining
"""


class BudgetTrackerProtocol(Protocol):
    """预算跟踪器协议（内存版与 Redis 版共同契约）。

    ``consume``/``remaining``/``is_exhausted`` 三个方法语义见
    ``model_router.BudgetTracker`` 的 docstring。``budget=0`` 视为不限制
    （``is_exhausted`` 永远返回 False，``remaining`` 返回 0）。
    """

    def consume(self, tokens: int, *, now: float | None = None) -> None: ...
    def remaining(self, *, now: float | None = None) -> int: ...
    def is_exhausted(self, *, now: float | None = None) -> bool: ...


class RedisBudgetTracker:
    """Redis ZSET 滑动窗口 token 预算跟踪器（多 pod 共享）。

    用法：
        r = redis.from_url(settings.redis_url)  # 同步客户端
        tracker = RedisBudgetTracker(budget=1000, window_seconds=60, redis_client=r)
        tracker.consume(300)
        if tracker.is_exhausted():
            ...  # 熔断降级

    注意：使用同步 ``redis.Redis`` 客户端（非 ``redis.asyncio``），因为
    ``BudgetTracker`` 接口是同步的，且 ``consume`` 在请求路径只调用一次，
    阻塞开销可接受（Redis ZADD/HSET 单次 ~1ms）。
    """

    def __init__(
        self,
        budget: int,
        window_seconds: float,
        redis_client: Any,
        *,
        namespace: str = "agent_cost",
    ) -> None:
        self._budget = max(0, budget)
        self._window = max(1.0, window_seconds)
        self._redis = redis_client
        # 命名空间隔离不同 budget 实例（如未来按 model 分桶）。Key 用冒号分隔
        # 符合 Redis 社区惯例（redis-cli --scan / RedisInsight 可读）。
        self._zset_key = f"budget:{namespace}:events"
        self._hash_key = f"budget:{namespace}:tokens"
        # register_script 返回 Script 对象，调用时自动处理 NOSCRIPT 重发
        self._consume_script = redis_client.register_script(_LUA_CONSUME)
        self._remaining_script = redis_client.register_script(_LUA_REMAINING)

    def consume(self, tokens: int, *, now: float | None = None) -> None:
        """记录一次 token 消耗。失败仅记日志，不抛异常（不阻塞主流程）。

        tokens <= 0 时 no-op（与内存版语义一致）。
        """
        if tokens <= 0:
            return
        ts = now if now is not None else time.time()
        event_id = str(uuid.uuid4())
        try:
            self._consume_script(
                keys=[self._zset_key, self._hash_key],
                args=[ts, self._window, event_id, tokens, self._budget],
            )
        except Exception:  # noqa: BLE001
            # 预算跟踪失败不应阻塞主请求路径——降级为"本次不计数"，
            # 后续 remaining() 会因本次未入账而偏高，但比抛异常影响小。
            logger.warning(
                "RedisBudgetTracker.consume 失败（本次 token=%d 未入账）",
                tokens,
                exc_info=True,
            )

    def remaining(self, *, now: float | None = None) -> int:
        """返回窗口内剩余预算。

        budget=0 时返回 0（与内存版语义一致：0 = 不限制，但 remaining 数值
        显示为 0）。Redis 失败时返回 0 触发熔断（保守策略：宁可误熔断）。

        与内存版的差异：内存版失败不可能发生，Redis 版可能因网络抖动失败，
        返回 0 让 ``is_exhausted`` 触发降级到 cheap model——这是显式的设计
        选择：成本失控比短暂降级更危险。
        """
        if self._budget == 0:
            return 0
        ts = now if now is not None else time.time()
        try:
            return int(
                self._remaining_script(
                    keys=[self._zset_key, self._hash_key],
                    args=[ts, self._window, self._budget],
                )
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "RedisBudgetTracker.remaining 失败，返回 0 触发熔断", exc_info=True
            )
            return 0

    def is_exhausted(self, *, now: float | None = None) -> bool:
        """是否熔断（剩余 ≤ 0）。budget=0 视为不限制（永不熔断）。"""
        if self._budget == 0:
            return False
        return self.remaining(now=now) <= 0


async def build_budget_tracker_from_settings() -> BudgetTrackerProtocol:
    """根据 settings 选择 budget 跟踪器实现。

    - ``agent_cost_budget_redis_enabled=True``：尝试 Redis，失败回退内存（带 warning）
    - 否则：内存版（与原行为一致，单测/CI 默认路径）

    工厂在 ``service._get_budget_tracker`` 调用，单测 monkeypatch 此函数或
    ``_budget_tracker`` 单例即可覆盖。

    P3-4：``client.ping()`` 是同步阻塞调用,用 ``asyncio.to_thread`` 包装
    避免阻塞 event loop（socket_timeout=2s 最坏阻塞 2s）。
    """
    from app.core.config import settings

    if not settings.agent_cost_budget_redis_enabled:
        # 默认路径：内存版（与历史行为一致，单测/CI 不依赖 Redis）
        from app.domains.agents.model_router import BudgetTracker

        return BudgetTracker(
            settings.agent_cost_token_budget,
            settings.agent_cost_budget_window_seconds,
        )

    # 生产路径：Redis ZSET 多 pod 共享。Redis 不可达时回退内存版（带 warning），
    # 保证 dev/CI/Redis 故障期仍可用，但失去多 pod 共享语义——这是显式降级，
    # 不应在生产长期运行（监控 RedisBudgetTracker 失败率告警）。
    try:
        import redis as sync_redis

        client = sync_redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_timeout=2.0,  # 短超时：请求路径不能等 Redis 慢查询
            socket_connect_timeout=2.0,
        )
        # P3-4：同步 ping 放线程池,避免阻塞 event loop
        await asyncio.to_thread(client.ping)
        return RedisBudgetTracker(
            budget=settings.agent_cost_token_budget,
            window_seconds=settings.agent_cost_budget_window_seconds,
            redis_client=client,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Redis 不可达，BudgetTracker 回退到内存实现——"
            "多 pod 预算共享失效，仅适用于 dev/Redis 故障期",
            exc_info=True,
        )
        from app.domains.agents.model_router import BudgetTracker

        return BudgetTracker(
            settings.agent_cost_token_budget,
            settings.agent_cost_budget_window_seconds,
        )


__all__ = [
    "BudgetTrackerProtocol",
    "RedisBudgetTracker",
    "build_budget_tracker_from_settings",
]
