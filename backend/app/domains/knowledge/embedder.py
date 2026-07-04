"""向量化封装 — 调用 OpenAI Embeddings API。

失败回退零向量（保证文档上传不中断，向量检索时零向量自然排末位）。

P3：共享 ``httpx.AsyncClient`` 单例（原每次调用新建客户端，大文档批量向量化时
多次 TCP 握手 + TLS 协商）。应用关闭时由 ``close_embedder_client`` 释放。

P1-7：向量维度解耦。``EMBEDDING_MODEL_REGISTRY`` 显式声明每个模型产出的维度，
embedder 不再硬编码 1536。``embed_text`` / ``embed_batch`` 返回 API 实际维度
的向量，调用方（service 层）负责校验维度与 chunks.embedding 列维度一致，
避免静默维度不匹配导致 pgvector 写入或检索崩溃。
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_URL = "https://api.openai.com/v1/embeddings"

# P1-7：embedding 模型 → 维度注册表。新增模型在此登记即可，无需改 embedder 逻辑。
# 维度需与 chunks.embedding 列的 Vector(N) 一致，否则 pgvector 写入/检索崩溃。
EMBEDDING_MODEL_REGISTRY: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def get_embedding_dim(model: str) -> int:
    """返回模型产出维度。未登记模型抛 KeyError（调用方应在校验阶段拦截）。"""
    return EMBEDDING_MODEL_REGISTRY[model]

# 共享客户端单例（懒初始化）。参考 core/redis.py 的单例模式。
# 测试环境 monkeypatch _client 即可替换。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """返回共享的 httpx.AsyncClient（懒初始化）。

    连接池复用避免每次 embedding 重建 TCP/TLS；``is_closed`` 守卫处理
    测试中显式 aclose 后重建的场景。
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_embedder_client() -> None:
    """关闭共享客户端（应用 lifespan 关闭时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def embed_text(text: str, model: str = DEFAULT_EMBEDDING_MODEL) -> list[float]:
    """对单段文本向量化。失败返回零向量（维度按模型注册表推断）。"""
    dim = get_embedding_dim(model)
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY 未配置，返回零向量")
        return _zero_vector(dim)
    try:
        client = _get_client()
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
        return _zero_vector(dim)


async def embed_batch(
    texts: list[str], model: str = DEFAULT_EMBEDDING_MODEL
) -> list[list[float]]:
    """批量向量化。单次 API 调用批量传入（OpenAI 单次限 2048 输入）。

    失败的批次回退零向量，保证文档上传不中断。
    """
    if not texts:
        return []
    dim = get_embedding_dim(model)
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY 未配置，返回零向量")
        return [_zero_vector(dim) for _ in texts]
    out: list[list[float] | None] = [None] * len(texts)
    batch_size = 2048
    client = _get_client()
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        try:
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
    return [vec if vec is not None else _zero_vector(dim) for vec in out]


def _zero_vector(dim: int = 1536) -> list[float]:
    """零向量（检索时 cosine 相似度最低，自然排末位）。

    P1-7：``dim`` 参数默认 1536 仅为向后兼容，调用方应显式传入模型对应维度。
    """
    return [0.0] * dim
