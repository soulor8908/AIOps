"""A2A 异步消息总线（P2-9）— Redis stream，Agent 间异步通信。

设计要点：
- ``A2AMessage``：消息信封（from/to/content/reply_to/correlation_id）。
- ``A2ABus``：基于 Redis stream 的异步消息总线。
  - ``publish``：XADD 到 ``a2a:{to_agent_id}`` 流。
  - ``consume``：XREAD 消费 ``a2a:{agent_id}`` 流，回调处理每条消息。
  - ``send_and_wait``：publish 后在 ``a2a:reply:{correlation_id}`` 等待响应。
    超时返回 None（不抛错）。
- 复用 ``app/core/redis.py`` 的 ``get_redis`` 单例。Redis 不可用时所有操作
  降级（publish 返回 None、consume 不启动、send_and_wait 返回 None），不阻塞主流程。
- 与 P3-12 的同步 ``agent_delegate`` 互补：同步委托用于即时子任务，异步消息
  用于长任务/解耦通信（如通知、事件驱动）。
- correlation_id 用 uuid4，保证 send_and_wait 的请求-响应匹配。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import redis.asyncio as redis

from app.core.redis import get_redis

# Redis xread 返回类型：
# list[tuple[stream_key, list[tuple[entry_id, fields_dict]]]]
# stream_key / entry_id 在 decode_responses=False 时为 bytes，fields_dict 为 dict[bytes, bytes]。
# redis-py 的类型 stub 推断为 ``Any | int | str``，mypy 无法 unpack，用 cast 显式标注。
StreamEntries = list[tuple[Any, list[tuple[Any, dict[Any, Any]]]]]

logger = logging.getLogger("app.agents.a2a")

# Redis stream key 前缀
_STREAM_PREFIX = "a2a:"
_REPLY_PREFIX = "a2a:reply:"
# 默认 send_and_wait 超时（秒）
_DEFAULT_TIMEOUT = 30.0
# consume 默认 block 毫秒
_BLOCK_MS = 5000
# stream 最大长度（XADD MAXLEN ~，防止无限增长）
_MAX_STREAM_LEN = 10000


@dataclass(slots=True)
class A2AMessage:
    """A2A 消息信封。"""

    from_agent: str
    to_agent: str
    content: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reply_to: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> A2AMessage:
        return cls(**json.loads(raw))


class A2ABus:
    """Redis stream 实现的 A2A 异步消息总线。

    所有 Redis 操作捕获异常并降级，不阻塞主流程。``redis_client`` 可注入
    以便测试用 fakeredis 替换。
    """

    def __init__(self, redis_client: redis.Redis | None = None) -> None:
        self._redis = redis_client

    def _client(self) -> redis.Redis:
        """惰性获取 Redis 单例（允许构造时不连 Redis）。"""
        return self._redis if self._redis is not None else get_redis()

    async def publish(self, message: A2AMessage) -> str | None:
        """发布消息到目标 agent 的流。返回 stream entry id，失败返回 None。"""
        try:
            client = self._client()
            stream = f"{_STREAM_PREFIX}{message.to_agent}"
            entry_id = await client.xadd(
                stream,
                {"data": message.to_json()},
                maxlen=_MAX_STREAM_LEN,
                approximate=True,
            )
            logger.debug(
                "A2A publish %s→%s (entry=%s)", message.from_agent, message.to_agent, entry_id
            )
            return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        except redis.RedisError:
            logger.exception("A2A publish failed %s→%s", message.from_agent, message.to_agent)
            return None
        except Exception:  # noqa: BLE001
            logger.exception("A2A publish unexpected error")
            return None

    async def consume(
        self,
        agent_id: str,
        handler: Any,
        *,
        count: int = 10,
        block_ms: int = _BLOCK_MS,
    ) -> None:
        """消费 agent 的消息流，回调处理每条消息。

        阻塞循环：XREAD block 等待新消息，回调抛异常时记日志不中断循环。
        Redis 不可用时记日志并返回（不启动循环）。

        注意：``block_ms=0`` 在 Redis 语义中表示永久阻塞，此处强制下限 1ms
        以避免 consume 永久挂起无法被取消的风险（虽 asyncio.CancelledError 仍可
        唤醒，但保险起见仍校验）。
        """
        if block_ms < 1:
            block_ms = 1
        try:
            client = self._client()
            stream = f"{_STREAM_PREFIX}{agent_id}"
            last_id = "0"  # 从头消费（生产应持久化 last_id）
            logger.info("A2A consume start for agent=%s", agent_id)
            while True:
                try:
                    resp = cast(
                        StreamEntries,
                        await client.xread({stream: last_id}, count=count, block=block_ms),
                    )
                except redis.RedisError:
                    logger.exception("A2A xread failed, retry in 1s")
                    await asyncio.sleep(1.0)
                    continue
                if not resp:
                    continue
                for _stream, entries in resp:
                    for entry_id, fields in entries:
                        last_id = (
                            entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                        )
                        raw = fields.get(b"data") or fields.get("data")
                        if raw is None:
                            continue
                        raw_str = raw.decode() if isinstance(raw, bytes) else raw
                        try:
                            msg = A2AMessage.from_json(raw_str)
                            await handler(msg)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "A2A handler error (agent=%s, entry=%s)", agent_id, last_id
                            )
        except asyncio.CancelledError:
            logger.info("A2A consume cancelled for agent=%s", agent_id)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("A2A consume fatal error (agent=%s)", agent_id)

    async def send_and_wait(
        self,
        target_agent: str,
        content: str,
        *,
        from_agent: str = "system",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> A2AMessage | None:
        """发布消息并等待响应（请求-响应模式）。

        在 ``a2a:reply:{correlation_id}`` 流上等待响应。超时返回 None。
        适用于需要结果但可异步等待的场景。生产应在目标 agent 的 consume
        handler 中 publish reply 到 reply_to 流。
        """
        correlation_id = str(uuid.uuid4())
        reply_stream = f"{_REPLY_PREFIX}{correlation_id}"
        msg = A2AMessage(
            from_agent=from_agent,
            to_agent=target_agent,
            content=content,
            correlation_id=correlation_id,
            reply_to=reply_stream,
        )
        published = await self.publish(msg)
        if published is None:
            return None
        # 等待响应
        try:
            client = self._client()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "A2A send_and_wait timeout (corr=%s, target=%s)",
                        correlation_id, target_agent,
                    )
                    return None
                # block=0 在 Redis 语义中表示永久阻塞；当剩余时间不足 1ms 时
                # 直接判定超时，避免 xread 永久挂起。
                block_ms = int(remaining * 1000)
                if block_ms < 1:
                    logger.warning(
                        "A2A send_and_wait timeout (corr=%s, target=%s)",
                        correlation_id, target_agent,
                    )
                    return None
                block = min(block_ms, _BLOCK_MS)
                resp = cast(
                    StreamEntries,
                    await client.xread({reply_stream: "0"}, count=1, block=block),
                )
                if not resp:
                    continue
                for _stream, entries in resp:
                    for _eid, fields in entries:
                        raw = fields.get(b"data") or fields.get("data")
                        if raw is None:
                            continue
                        raw_str = raw.decode() if isinstance(raw, bytes) else raw
                        return A2AMessage.from_json(raw_str)
                return None
        except redis.RedisError:
            logger.exception("A2A send_and_wait redis error (corr=%s)", correlation_id)
            return None
        except Exception:  # noqa: BLE001
            logger.exception("A2A send_and_wait unexpected error")
            return None

    async def reply(self, original: A2AMessage, content: str) -> str | None:
        """回复一条消息：publish 到 original.reply_to 流。"""
        if not original.reply_to:
            logger.warning("A2A reply: original message has no reply_to")
            return None
        reply_msg = A2AMessage(
            from_agent=original.to_agent,
            to_agent=original.from_agent,
            content=content,
            correlation_id=original.correlation_id,
            reply_to=None,
        )
        try:
            client = self._client()
            entry_id = await client.xadd(
                original.reply_to,
                {"data": reply_msg.to_json()},
                maxlen=_MAX_STREAM_LEN,
                approximate=True,
            )
            return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
        except redis.RedisError:
            logger.exception("A2A reply failed (corr=%s)", original.correlation_id)
            return None
        except Exception:  # noqa: BLE001
            logger.exception("A2A reply unexpected error")
            return None


__all__ = [
    "A2ABus",
    "A2AMessage",
]
