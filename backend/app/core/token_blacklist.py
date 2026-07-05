"""JWT token 黑名单（security.spec.md§2.2）。

登出时把 token 的 ``jti`` 写入 Redis,TTL = 剩余有效期。后续请求校验 token
时检查 ``jti`` 是否在黑名单——在则拒绝（视为已登出）。

降级策略：Redis 不可用时 ``is_revoked`` 返回 ``False``（放行），仅记 warning。
原因：黑名单依赖 Redis 可达，Redis 故障时若拒绝所有 token 会导致全员无法
认证（系统不可用），违背可用性优先原则。已登出的 token 在 Redis 恢复前
仍可用——这是可接受的窗口（TTL 自然过期兜底）。

测试环境无 Redis（conftest ``_skip_rate_limit`` 让 ``get_redis`` 抛异常），
``is_revoked`` 自动降级放行，单测不依赖 Redis。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger("app.core.token_blacklist")

# Redis key 前缀。jti 是 UUID，全局唯一，无需按用户分桶。
_PREFIX = "jwt:blacklist:"


async def revoke_token(jti: str, expires_at: int | None = None) -> None:
    """把 token jti 加入黑名单。

    ``expires_at`` 为 token 的 exp（Unix 秒）。TTL = exp - now，使黑名单
    条目在 token 自然过期后自动清理，避免无限累积。未提供时用 access token
    最大有效期兜底。
    """
    import time

    if not settings.token_blacklist_enabled:
        return
    if not jti:
        return
    now = int(time.time())
    if expires_at is not None and expires_at > now:
        ttl = expires_at - now
    else:
        # 兜底：access token 最长有效期。过期 token 本身就无效，无需长期留存。
        ttl = settings.access_token_expire_seconds
    try:
        await get_redis().set(f"{_PREFIX}{jti}", "1", ex=ttl)
    except Exception:  # noqa: BLE001
        logger.warning(
            "token_blacklist.revoke 失败（jti=%s）Redis 不可用，"
            "该 token 在 Redis 恢复前仍可用",
            jti,
            exc_info=True,
        )


async def is_revoked(jti: str) -> bool:
    """检查 token jti 是否在黑名单。

    Redis 不可用或开关关闭时返回 ``False``（降级放行）。
    """
    if not settings.token_blacklist_enabled:
        return False
    if not jti:
        return False
    try:
        result = await get_redis().get(f"{_PREFIX}{jti}")
        return result is not None
    except Exception:  # noqa: BLE001
        logger.warning(
            "token_blacklist.is_revoked 失败（jti=%s）Redis 不可用，降级放行",
            jti,
            exc_info=True,
        )
        return False


__all__ = ["is_revoked", "revoke_token"]
