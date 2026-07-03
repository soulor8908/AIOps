"""Redis 客户端 — 限流等异步操作使用（security.spec.md§5）。

懒初始化：首次 ``get_redis()`` 时创建连接池。测试环境通过 monkeypatch
``get_redis`` 返回 fakeredis 实例覆盖。
"""

from __future__ import annotations

import redis.asyncio as redis

from app.core.config import settings

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """返回缓存的 Redis 连接单例（懒初始化）。"""
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=False)
    return _redis


async def close_redis() -> None:
    """关闭 Redis 连接池（应用关闭时调用）。"""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
