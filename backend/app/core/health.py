"""依赖健康检查（deployment.spec.md§6）。

为 ``/health`` 端点提供 DB / Redis 可达性探测：

- 单项探测带 ``_CHECK_TIMEOUT`` 超时，避免 K8s probe 因依赖卡死而超时。
- 异常一律视为不可达（返回 ``False``），由端点汇总为 ``degraded`` 状态；
  响应状态码始终 200，readiness probe 据此摘流而非重启（§6）。
- 探测函数为模块级纯协程，便于单测 monkeypatch 与端点测试注入。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.database import engine
from app.core.redis import get_redis

# 单项探测超时（秒）。K8s readiness 默认 timeoutSeconds=1~3，探测须快于其。
_CHECK_TIMEOUT = 2.0


async def check_db() -> bool:
    """数据库可达性：``SELECT 1``。"""
    try:
        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")), timeout=_CHECK_TIMEOUT
            )
        return True
    except Exception:
        return False


async def check_redis() -> bool:
    """Redis 可达性：``PING``。"""
    try:
        return bool(
            await asyncio.wait_for(get_redis().ping(), timeout=_CHECK_TIMEOUT)
        )
    except Exception:
        return False
