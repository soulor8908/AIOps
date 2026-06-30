"""向量化封装 — 调用 OpenAI Embeddings API。

失败回退零向量（保证文档上传不中断，向量检索时零向量自然排末位）。
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings
from app.domains.knowledge.models import EMBEDDING_DIM

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_URL = "https://api.openai.com/v1/embeddings"


async def embed_text(text: str, model: str = DEFAULT_EMBEDDING_MODEL) -> list[float]:
    """对单段文本向量化。失败返回零向量。"""
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY 未配置，返回零向量")
        return _zero_vector()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                EMBEDDING_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": model, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return list(map(float, data["data"][0]["embedding"]))
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        logger.error("向量化失败，回退零向量: %s", exc)
        return _zero_vector()


async def embed_batch(
    texts: list[str], model: str = DEFAULT_EMBEDDING_MODEL
) -> list[list[float]]:
    """批量向量化。单次 API 调用批量传入（OpenAI 单次限 2048 输入）。

    失败的批次回退零向量，保证文档上传不中断。
    """
    if not texts:
        return []
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY 未配置，返回零向量")
        return [_zero_vector() for _ in texts]
    out: list[list[float] | None] = [None] * len(texts)
    batch_size = 2048
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    EMBEDDING_URL,
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={"model": model, "input": batch},
                )
                resp.raise_for_status()
                data = resp.json()
                for i, item in enumerate(data["data"]):
                    out[start + i] = list(map(float, item["embedding"]))
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            logger.error("批量向量化失败，回退零向量: %s", exc)
    return [vec if vec is not None else _zero_vector() for vec in out]


def _zero_vector() -> list[float]:
    """零向量（检索时 cosine 相似度最低，自然排末位）。"""
    return [0.0] * EMBEDDING_DIM
