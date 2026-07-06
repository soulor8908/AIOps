"""Redis 滑动窗口限流中间件（security.spec.md§5）。

策略：
- per user（已认证）：默认 100 req/min
- per IP（未认证）：默认 100 req/min
- LLM 端点（/chat /execute /run /rag）：20 req/min

超限返回 ``429 rate_limited`` + ``X-RateLimit-Limit/Remaining/Reset`` 响应头。
Redis 不可用时降级到本地滑动窗口桶（P0-18），避免 Redis 故障期间限流失效
导致 LLM 端点被滥用。本地桶是 per-pod 的，多 pod 部署时实际限额为
``limit * pod_count``——降级模式下的可接受妥协。

滑动窗口算法使用 Redis ZSET：每个请求时间戳作为 member+score，
先清除窗口外过期条目，再添加当前请求，最后计数判断是否超限。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import jwt as pyjwt
from jwt import PyJWTError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger("app.core.rate_limit")

# security.spec.md§5.1 — 默认配额与窗口。
DEFAULT_LIMIT = 100  # req/min per user / per IP
LLM_LIMIT = 20  # req/min for LLM 端点
WINDOW_SECONDS = 60

# LLM 推理端点路径后缀（security.spec.md§5.1「调用模型推理的端点」）。
# 命中这些后缀的请求使用更严格的 20/min 配额（成本与资源敏感）。
_LLM_PATH_SUFFIXES = frozenset({"/chat", "/execute", "/run", "/rag"})


# P0-18：本地降级桶。Redis 故障时启用，避免限流失效。
# per-pod 滑动窗口：每个 key 维护窗口内时间戳列表，超限返回 429。
# 多 pod 部署时每个 pod 独立计数，实际限额 = limit * pod_count（降级妥协）。
_local_buckets: dict[str, list[float]] = {}
_local_buckets_lock = asyncio.Lock()
# 本地桶 key 数量上限，防止 key 无限增长（每个 key 一个 list）。
# 超限时清空最旧的 key（简单 LRU 近似——dict 保序，clear 全量重置）。
_LOCAL_BUCKETS_MAX_KEYS = 10000


def _is_llm_endpoint(path: str) -> bool:
    """判断路径是否为 LLM 推理端点（适用 20/min 配额）。"""
    return any(path.endswith(suffix) for suffix in _LLM_PATH_SUFFIXES)


def _extract_user_id(scope: Scope) -> str | None:
    """从 Authorization Bearer token 解码 user_id（验签 + 验过期，仅用于限流 keying）。

    实际认证校验由路由依赖 ``get_current_user`` 完成。此处仅提取 ``sub`` claim
    作为限流维度——即使伪造 token 也无法绕过限流（配额按 user_id 计），
    且伪造 token 在路由层会被拒绝。

    P2：启用 ``verify_exp``——过期 token 不应消耗目标用户配额（攻击者持有泄露的
    过期 token 可借限流 keying 挤占真实用户配额）。过期/无效 token 退化为按 IP 限流。
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header = value.decode("latin-1")
            if not header.startswith("Bearer "):
                return None
            token = header[7:]
            try:
                payload = pyjwt.decode(
                    token,
                    settings.effective_secret_key,
                    algorithms=["HS256"],
                    options={"verify_exp": True},
                )
                sub = payload.get("sub")
                return sub if isinstance(sub, str) else None
            except PyJWTError:
                # 限流 keying 容错：token 无效/过期/格式错误时退化为按 IP 限流。
                # 实际认证由 get_current_user 路由依赖完成。
                return None
    return None


def _extract_ip(scope: Scope) -> str:
    """提取客户端 IP（优先 X-Forwarded-For，回退 client）。"""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            forwarded: str = value.decode("latin-1")
            return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _check_sliding_window(
    redis_client: Any,
    key: str,
    limit: int,
    window: int,
) -> tuple[bool, int, int]:
    """滑动窗口限流检查（Redis ZSET）。

    返回 ``(allowed, remaining, reset_timestamp)``。
    """
    now = time.time()
    window_start = now - window
    # member 用 ``now:uuid`` 保证唯一，避免高并发/时钟回退时时间戳碰撞导致
    # ZSET 覆盖（zadd 对已存在 member 仅更新 score），从而计数少计、限流被绕过。
    member = f"{now}:{uuid.uuid4()}"
    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)  # 清除窗口外过期条目
    pipe.zadd(key, {member: now})  # 添加当前请求时间戳（score 仍为 now）
    pipe.zcard(key)  # 计数窗口内条目
    pipe.expire(key, window)  # 设置 TTL 防止 key 泄漏
    results = await pipe.execute()
    count: int = results[2]
    allowed = count <= limit
    remaining = max(0, limit - count)
    reset = int(now + window)
    return allowed, remaining, reset


async def _check_local_sliding_window(
    key: str, limit: int, window: int
) -> tuple[bool, int, int]:
    """P0-18：本地滑动窗口限流检查（Redis 故障降级用）。

    与 ``_check_sliding_window`` 算法一致，但状态存进程内存 ``_local_buckets``。
    加锁保护"清除+添加+计数"原子性（asyncio 单线程但 await 间可切换）。
    key 数量超 ``_LOCAL_BUCKETS_MAX_KEYS`` 时全量清空，防内存泄漏。
    """
    now = time.time()
    window_start = now - window
    async with _local_buckets_lock:
        if len(_local_buckets) > _LOCAL_BUCKETS_MAX_KEYS:
            # 简单清理：全量清空。降级路径下偶发重置可接受（最坏情况是
            # 重置瞬间所有 key 配额刷新，短暂放宽——优于 OOM）。
            _local_buckets.clear()
        bucket = _local_buckets.get(key)
        if bucket is None:
            bucket = []
            _local_buckets[key] = bucket
        # 清除窗口外过期条目
        bucket[:] = [ts for ts in bucket if ts > window_start]
        bucket.append(now)
        count = len(bucket)
    allowed = count <= limit
    remaining = max(0, limit - count)
    reset = int(now + window)
    return allowed, remaining, reset


class RateLimitMiddleware:
    """纯 ASGI 限流中间件（security.spec.md§5）。

    对所有 ``/api/`` 路径生效；非 API 路径（/health /metrics /docs）不限流。
    """

    def __init__(
        self,
        app: ASGIApp,
        default_limit: int = DEFAULT_LIMIT,
        llm_limit: int = LLM_LIMIT,
        window: int = WINDOW_SECONDS,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.llm_limit = llm_limit
        self.window = window

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        # 非 API 路径不限流（健康检查、指标、文档）
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # 确定限流 key：已认证用 user_id，未认证用 IP（security.spec.md§5.1）
        # LLM 端点使用独立 key，与默认配额互不影响（20/min 与 100/min 分别计数）。
        user_id = _extract_user_id(scope)
        is_llm = _is_llm_endpoint(path)
        bucket = "llm" if is_llm else "default"
        if user_id:
            key = f"ratelimit:{bucket}:user:{user_id}"
        else:
            key = f"ratelimit:{bucket}:ip:{_extract_ip(scope)}"

        limit = self.llm_limit if is_llm else self.default_limit

        try:
            redis_client = get_redis()
            allowed, remaining, reset = await _check_sliding_window(
                redis_client, key, limit, self.window
            )
        except Exception:
            # P0-18：Redis 不可用 — 降级到本地滑动窗口桶，避免限流失效。
            # 本地桶 per-pod 计数，多 pod 实际限额 = limit * pod_count（降级妥协）。
            # 本地桶也失败（极端情况）才放行，确保不阻断请求。
            logger.warning("Redis 不可用，降级到本地限流桶", exc_info=True)
            try:
                allowed, remaining, reset = await _check_local_sliding_window(
                    key, limit, self.window
                )
            except Exception:  # noqa: BLE001
                logger.exception("本地限流桶也失败，放行请求")
                await self.app(scope, receive, send)
                return

        if not allowed:
            # 超限 → 429 + 限流头（errors.spec.md§4 / security.spec.md§5.2）
            response = JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": "请求频率超限，请稍后重试",
                },
            )
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["X-RateLimit-Reset"] = str(reset)
            await response(scope, receive, send)
            return

        # 未超限 → 注入限流头到响应
        limit_str = str(limit).encode("latin-1")
        remaining_str = str(remaining).encode("latin-1")
        reset_str = str(reset).encode("latin-1")

        async def send_with_ratelimit(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", limit_str))
                headers.append((b"x-ratelimit-remaining", remaining_str))
                headers.append((b"x-ratelimit-reset", reset_str))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_ratelimit)
