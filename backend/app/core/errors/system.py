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
