"""依赖健康检查（deployment.spec.md§6）。

为 ``/health`` 端点提供 DB / Redis / LLM 可达性探测：

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


async def check_llm() -> bool:
    """P0-19：LLM 可达性探测。

    发一个 ``max_tokens=1`` 的极短请求验证 provider 可达 + API key 有效。
    生产 readiness probe 每 10s 调用一次，单次成本约 1 token（~$0.0001），
    可接受。``max_retries=0`` 避免重试拖长探测时间。

    API key 未配置时返回 ``True``（跳过）——P0-13 fail-fast 启动校验已在
    生产环境强制要求 API key，此处跳过仅影响开发/测试环境（无 key 时不
    把 /health 标记为 degraded）。
    """
    from app.core.config import settings
    from app.core.llm_client import LLMClient, LLMConfig, Message

    api_key = (
        settings.openai_api_key
        if settings.default_llm_provider == "openai"
        else settings.anthropic_api_key
    )
    if not api_key:
        return True  # 跳过，不算 down

    config = LLMConfig(
        provider=settings.default_llm_provider,  # type: ignore[arg-type]
        model=settings.default_llm_model,
        api_key=api_key,
        max_tokens=1,
        temperature=0.0,
    )
    try:
        async with LLMClient(config, timeout=_CHECK_TIMEOUT, max_retries=0) as client:
            await asyncio.wait_for(
                client.chat([Message(role="user", content=".")]),
                timeout=_CHECK_TIMEOUT,
            )
        return True
    except Exception:
        return False
