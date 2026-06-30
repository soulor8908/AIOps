"""系统级异常 — 限流 / LLM 调用失败。"""

from __future__ import annotations

from app.core.errors.base import AppError


class RateLimitError(AppError):
    """触发限流 (429)。"""

    status_code = 429
    error_code = "rate_limited"
    message = "请求过于频繁，请稍后重试"


class LLMError(AppError):
    """LLM 调用失败 (502)。"""

    status_code = 502
    error_code = "llm_error"
    message = "LLM 调用失败"
