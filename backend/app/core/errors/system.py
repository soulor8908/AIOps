"""系统级异常 — 限流 / LLM 调用失败 / 向量化失败。"""

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


class EmbeddingError(AppError):
    """向量化失败 (502)。

    A4：``embed_text`` / ``embed_batch`` 的 ``strict=True`` 模式下抛出。
    用于文档上传路径——失败时 ``upload_document`` 据此将 ``Document.status``
    置为 ``failed`` 并跳过 chunk 写入，避免零向量污染向量索引。
    """

    status_code = 502
    error_code = "embedding_error"
    message = "向量化失败"


class GatewayTimeoutError(AppError):
    """P0-20：请求级超时 (504)。

    长耗时端点（execute_agent / execute_workflow / rag_query）超
    ``agent_execute_timeout_seconds`` 时抛出。客户端已断开或等待过久，
    服务端继续跑只会产生 LLM 成本但结果丢弃。
    """

    status_code = 504
    error_code = "gateway_timeout"
    message = "请求处理超时，请稍后重试或缩小请求范围"
