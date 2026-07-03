"""Redis 滑动窗口限流中间件（security.spec.md§5）。

策略：
- per user（已认证）：默认 100 req/min
- per IP（未认证）：默认 100 req/min
- LLM 端点（/chat /execute /run /rag）：20 req/min

超限返回 ``429 rate_limited`` + ``X-RateLimit-Limit/Remaining/Reset`` 响应头。
Redis 不可用时降级放行（log warning），不阻断请求。

滑动窗口算法使用 Redis ZSET：每个请求时间戳作为 member+score，
先清除窗口外过期条目，再添加当前请求，最后计数判断是否超限。
"""

from __future__ import annotations

import logging
import time
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


def _is_llm_endpoint(path: str) -> bool:
    """判断路径是否为 LLM 推理端点（适用 20/min 配额）。"""
    return any(path.endswith(suffix) for suffix in _LLM_PATH_SUFFIXES)


def _extract_user_id(scope: Scope) -> str | None:
    """从 Authorization Bearer token 解码 user_id（不验证签名，仅用于限流 keying）。

    实际认证校验由路由依赖 ``get_current_user`` 完成。此处仅提取 ``sub`` claim
    作为限流维度——即使伪造 token 也无法绕过限流（配额按 user_id 计），
    且伪造 token 在路由层会被拒绝。
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
                    options={"verify_exp": False},
                )
                sub = payload.get("sub")
                return sub if isinstance(sub, str) else None
            except PyJWTError:
                # 限流 keying 容错：token 无效/格式错误时退化为按 IP 限流。
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
    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)  # 清除窗口外过期条目
    pipe.zadd(key, {str(now): now})  # 添加当前请求时间戳
    pipe.zcard(key)  # 计数窗口内条目
    pipe.expire(key, window)  # 设置 TTL 防止 key 泄漏
    results = await pipe.execute()
    count: int = results[2]
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
            # Redis 不可用 — 降级放行（不阻断请求，仅记录警告）
            logger.warning("Redis 不可用，限流跳过", exc_info=True)
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
