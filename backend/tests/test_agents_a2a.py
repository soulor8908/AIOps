"""A2A 异步消息（P2-9）测试 — Redis stream，Agent 间异步通信。

覆盖：
1. **A2AMessage**：序列化/反序列化
2. **A2ABus.publish**：XADD 写入 stream + Redis 失败降级
3. **A2ABus.consume**：XREAD 消费 + handler 回调 + 异常隔离
4. **A2ABus.send_and_wait**：请求-响应 + 超时返回 None
5. **A2ABus.reply**：回复到 reply_to 流
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import redis.asyncio as redis

from app.domains.agents.a2a import (
    A2ABus,
    A2AMessage,
)

# ===================== 1. A2AMessage =====================


def test_message_to_from_json_roundtrip() -> None:
    msg = A2AMessage(from_agent="a", to_agent="b", content="hello")
    raw = msg.to_json()
    restored = A2AMessage.from_json(raw)
    assert restored.from_agent == "a"
    assert restored.to_agent == "b"
    assert restored.content == "hello"
    assert restored.correlation_id == msg.correlation_id


def test_message_default_correlation_id_unique() -> None:
    m1 = A2AMessage(from_agent="a", to_agent="b", content="x")
    m2 = A2AMessage(from_agent="a", to_agent="b", content="x")
    assert m1.correlation_id != m2.correlation_id


def test_message_reply_to_defaults_none() -> None:
    msg = A2AMessage(from_agent="a", to_agent="b", content="x")
    assert msg.reply_to is None


# ===================== 2. publish =====================


async def test_publish_writes_to_stream() -> None:
    """publish 成功写入 Redis stream，返回 entry id。"""
    fake = fakeredis.aioredis.FakeRedis()
    bus = A2ABus(redis_client=fake)
    msg = A2AMessage(from_agent="a", to_agent="b", content="hello")
    entry_id = await bus.publish(msg)
    assert entry_id is not None
    # 验证 stream 有数据
    info = await fake.xlen("a2a:b")
    assert info == 1
    await fake.aclose()


async def test_publish_redis_failure_returns_none() -> None:
    """Redis 异常时 publish 返回 None，不抛出。"""
    mock_redis = MagicMock(spec=redis.Redis)
    mock_redis.xadd = AsyncMock(side_effect=redis.RedisError("conn refused"))
    bus = A2ABus(redis_client=mock_redis)
    msg = A2AMessage(from_agent="a", to_agent="b", content="x")
    result = await bus.publish(msg)
    assert result is None


# ===================== 3. consume =====================


async def test_consume_calls_handler_for_messages() -> None:
    """consume 读取 stream 消息并回调 handler。"""
    fake = fakeredis.aioredis.FakeRedis()
    # 预写入 2 条消息
    await fake.xadd("a2a:agent1", {"data": json.dumps({
        "from_agent": "x", "to_agent": "agent1", "content": "m1",
        "correlation_id": "c1", "reply_to": None, "timestamp": 0,
    })})
    await fake.xadd("a2a:agent1", {"data": json.dumps({
        "from_agent": "x", "to_agent": "agent1", "content": "m2",
        "correlation_id": "c2", "reply_to": None, "timestamp": 0,
    })})

    bus = A2ABus(redis_client=fake)
    received: list[str] = []

    async def handler(msg: A2AMessage) -> None:
        received.append(msg.content)

    # 启动 consume 任务，处理完 2 条后取消
    task = asyncio.create_task(bus.consume("agent1", handler, block_ms=100))
    # 等待 handler 处理
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert received == ["m1", "m2"]
    await fake.aclose()


async def test_consume_handler_exception_does_not_stop_loop() -> None:
    """handler 抛异常时记日志但不中断 consume 循环。"""
    fake = fakeredis.aioredis.FakeRedis()
    await fake.xadd("a2a:agent1", {"data": json.dumps({
        "from_agent": "x", "to_agent": "agent1", "content": "bad",
        "correlation_id": "c1", "reply_to": None, "timestamp": 0,
    })})
    await fake.xadd("a2a:agent1", {"data": json.dumps({
        "from_agent": "x", "to_agent": "agent1", "content": "good",
        "correlation_id": "c2", "reply_to": None, "timestamp": 0,
    })})

    bus = A2ABus(redis_client=fake)
    received: list[str] = []

    async def handler(msg: A2AMessage) -> None:
        if msg.content == "bad":
            raise RuntimeError("handler boom")
        received.append(msg.content)

    task = asyncio.create_task(bus.consume("agent1", handler, block_ms=100))
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # 第二条仍被处理
    assert received == ["good"]
    await fake.aclose()


# ===================== 4. send_and_wait =====================


async def test_send_and_wait_returns_response() -> None:
    """send_and_wait 在超时内收到响应。"""
    fake = fakeredis.aioredis.FakeRedis()
    bus = A2ABus(redis_client=fake)

    # 模拟目标 agent 的 handler：收到请求后 reply
    async def responder(msg: A2AMessage) -> None:
        await bus.reply(msg, "response content")

    consumer_task = asyncio.create_task(
        bus.consume("target", responder, block_ms=100)
    )

    # 给 consumer 一点启动时间
    await asyncio.sleep(0.1)
    response = await bus.send_and_wait("target", "query", from_agent="caller", timeout=2.0)
    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task
    assert response is not None
    assert response.content == "response content"
    await fake.aclose()


async def test_send_and_wait_timeout_returns_none() -> None:
    """无响应时超时返回 None。"""
    fake = fakeredis.aioredis.FakeRedis()
    bus = A2ABus(redis_client=fake)
    # 不启动 consumer，直接 send_and_wait
    response = await bus.send_and_wait("nope", "q", from_agent="x", timeout=0.3)
    assert response is None
    await fake.aclose()


async def test_send_and_wait_publish_failure_returns_none() -> None:
    """publish 失败时 send_and_wait 立即返回 None。"""
    mock_redis = MagicMock(spec=redis.Redis)
    mock_redis.xadd = AsyncMock(side_effect=redis.RedisError("down"))
    bus = A2ABus(redis_client=mock_redis)
    response = await bus.send_and_wait("t", "q", timeout=1.0)
    assert response is None


# ===================== 5. reply =====================


async def test_reply_writes_to_reply_stream() -> None:
    """reply 写入 original.reply_to 流。"""
    fake = fakeredis.aioredis.FakeRedis()
    bus = A2ABus(redis_client=fake)
    original = A2AMessage(
        from_agent="caller",
        to_agent="target",
        content="req",
        reply_to="a2a:reply:corr-123",
    )
    entry_id = await bus.reply(original, "resp")
    assert entry_id is not None
    length = await fake.xlen("a2a:reply:corr-123")
    assert length == 1
    await fake.aclose()


async def test_reply_without_reply_to_returns_none() -> None:
    """original 无 reply_to 时 reply 返回 None。"""
    fake = fakeredis.aioredis.FakeRedis()
    bus = A2ABus(redis_client=fake)
    original = A2AMessage(from_agent="a", to_agent="b", content="x")  # reply_to=None
    result = await bus.reply(original, "resp")
    assert result is None
    await fake.aclose()


async def test_reply_redis_failure_returns_none() -> None:
    """Redis 异常时 reply 返回 None。"""
    mock_redis = MagicMock(spec=redis.Redis)
    mock_redis.xadd = AsyncMock(side_effect=redis.RedisError("down"))
    bus = A2ABus(redis_client=mock_redis)
    original = A2AMessage(
        from_agent="a", to_agent="b", content="x", reply_to="a2a:reply:c1"
    )
    result = await bus.reply(original, "resp")
    assert result is None
