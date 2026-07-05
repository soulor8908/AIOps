"""登录失败锁定（security.spec.md§6）。

连续失败 ``login_max_failures`` 次后锁定 ``login_lockout_minutes`` 分钟。
锁定期间拒绝登录（即使密码正确），抵抗暴力破解。

降级策略：Redis 不可用时所有检查降级放行（不锁定、不计失败次数），仅记 warning。
原因：锁定依赖 Redis 计数，Redis 故障时若强制锁定会导致全员无法登录（系统不可用）；
降级放行意味着暴力破解在 Redis 故障期间无防护——这是可接受的窗口（Redis 恢复后
恢复防护，且 rate_limit 中间件仍对登录端点有 per-IP 限流兜底）。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger("app.core.login_lockout")

_PREFIX = "login:fail:"
_LOCK_PREFIX = "login:lock:"


def _fail_key(email: str) -> str:
    return f"{_PREFIX}{email.lower()}"


def _lock_key(email: str) -> str:
    return f"{_LOCK_PREFIX}{email.lower()}"


async def check_lockout(email: str) -> bool:
    """检查账号是否被锁定。

    返回 ``True`` 表示已锁定（应拒绝登录）。Redis 不可用时返回 ``False``（放行）。
    """
    try:
        result = await get_redis().get(_lock_key(email))
        return result is not None
    except Exception:  # noqa: BLE001
        logger.warning(
            "login_lockout.check 失败（email=%s）Redis 不可用，降级放行",
            email,
            exc_info=True,
        )
        return False


async def record_failure(email: str) -> int:
    """记录一次登录失败，返回当前失败次数。达到阈值则设置锁定。

    Redis 不可用时返回 0（不计失败）。
    """
    try:
        client = get_redis()
        key = _fail_key(email)
        count = await client.incr(key)
        # 首次失败设置窗口 TTL（与锁定时长一致），使计数在锁定窗口后自动清零
        if count == 1:
            await client.expire(key, settings.login_lockout_minutes * 60)
        if count >= settings.login_max_failures:
            # 设置锁定 key，TTL = 锁定时长
            await client.set(
                _lock_key(email), "1", ex=settings.login_lockout_minutes * 60
            )
            logger.warning(
                "login_lockout 账号已锁定（email=%s, failures=%d, lock_minutes=%d）",
                email, count, settings.login_lockout_minutes,
            )
        return count
    except Exception:  # noqa: BLE001
        logger.warning(
            "login_lockout.record_failure 失败（email=%s）Redis 不可用，降级放行",
            email,
            exc_info=True,
        )
        return 0


async def record_success(email: str) -> None:
    """登录成功时清空失败计数（避免历史失败累积误锁）。"""
    try:
        await get_redis().delete(_fail_key(email))
    except Exception:  # noqa: BLE001
        # 静默失败：清计数失败不影响登录成功路径
        pass


__all__ = ["check_lockout", "record_failure", "record_success"]
