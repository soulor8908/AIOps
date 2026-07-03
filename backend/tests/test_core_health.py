"""健康检查探测函数单测（deployment.spec.md§6）。

直接测 ``check_db`` / ``check_redis`` 的可达性判定逻辑：
- 可达 → True；连接异常 / 超时 → False（不抛异常）。
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import app.core.health as health_mod


def test_check_db_returns_true_on_working_engine(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """可用 DB 引擎执行 SELECT 1 → True。"""
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    monkeypatch.setattr(health_mod, "engine", test_engine)

    async def _run() -> bool:
        try:
            return await health_mod.check_db()
        finally:
            await test_engine.dispose()

    assert asyncio.run(_run()) is True


def test_check_db_returns_false_on_connect_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """engine.connect() 抛错 → False（不向上传播）。"""

    class _BrokenEngine:
        def connect(self) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("db unreachable")

    monkeypatch.setattr(health_mod, "engine", _BrokenEngine())
    assert asyncio.run(health_mod.check_db()) is False


def test_check_redis_returns_true_with_fakeredis(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """fakeredis PING 成功 → True。"""
    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(health_mod, "get_redis", lambda: fake)
    assert asyncio.run(health_mod.check_redis()) is True


def test_check_redis_returns_false_on_connection_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """get_redis() 抛错 → False。"""

    def _raise() -> None:
        raise ConnectionRefusedError("no redis")

    monkeypatch.setattr(health_mod, "get_redis", _raise)
    assert asyncio.run(health_mod.check_redis()) is False


def test_check_redis_returns_false_on_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """PING 超过 _CHECK_TIMEOUT → False（wait_for 兜底）。"""

    class _HangingClient:
        async def ping(self) -> bool:  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return True

    monkeypatch.setattr(health_mod, "get_redis", lambda: _HangingClient())
    assert asyncio.run(health_mod.check_redis()) is False
