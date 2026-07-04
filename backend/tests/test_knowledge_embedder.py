"""knowledge/embedder.py 单元测试 — 向量化封装。

mock OpenAI API 调用，不依赖真实 API。

覆盖：
- embed_text 成功获取嵌入向量
- 无 API key 时返回零向量
- API 错误时返回零向量
- embed_batch 批量嵌入
- 向量维度为 1536
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

import app.domains.knowledge.embedder as embedder_mod
from app.core.config import settings
from app.domains.knowledge.embedder import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_URL,
    _zero_vector,
    embed_batch,
    embed_text,
)
from app.domains.knowledge.models import EMBEDDING_DIM


@pytest.fixture(autouse=True)
def _reset_embedder_shared_client() -> Any:
    """每个测试前重置共享 httpx.AsyncClient 单例。

    P3 后 embedder 使用共享客户端，若不重置则跨测试复用上一次的 MockTransport，
    导致断言失败（如 API key / model 不匹配）。
    """
    embedder_mod._client = None
    yield
    embedder_mod._client = None

# 在任何 patch 之前捕获真实的 httpx.AsyncClient 类引用。
# embedder 模块通过 ``import httpx`` 引用 httpx 模块，
# patch("app.domains.knowledge.embedder.httpx.AsyncClient") 会修改
# httpx 模块本身的 AsyncClient 属性（全局生效），因此 lambda 内部
# 如果通过 httpx.AsyncClient 查找会拿到已被替换的 mock → 无限递归。
# 用局部变量持有原始类引用即可避免此问题。
_RealAsyncClient = httpx.AsyncClient


# ===================== 辅助函数 =====================

def _patch_embedder_http(handler: Any) -> Any:
    """Patch httpx.AsyncClient in embedder 模块，使用 MockTransport。

    返回一个 ``patch`` 上下文管理器，将 embedder 内的
    ``httpx.AsyncClient`` 替换为使用 MockTransport 的工厂函数。
    """
    transport = httpx.MockTransport(handler)

    def _client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=transport, **kwargs)

    return patch(
        "app.domains.knowledge.embedder.httpx.AsyncClient",
        side_effect=_client_factory,
    )


# ===================== embed_text — 成功 =====================

async def test_embed_text_success() -> None:
    """成功获取嵌入向量。"""
    def handler(request: httpx.Request) -> httpx.Response:
        # 验证请求
        assert str(request.url) == EMBEDDING_URL
        assert request.headers["authorization"] == "Bearer test-key-embed"
        body = __import__("json").loads(request.content)
        assert body["model"] == DEFAULT_EMBEDDING_MODEL
        assert body["input"] == "hello world"
        return httpx.Response(200, json={
            "data": [{"embedding": [0.1] * EMBEDDING_DIM}],
        })

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key-embed"):
        result = await embed_text("hello world")

    assert len(result) == EMBEDDING_DIM
    assert result[0] == 0.1
    assert all(isinstance(v, float) for v in result)


async def test_embed_text_custom_model() -> None:
    """自定义 embedding 模型。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        captured["model"] = body["model"]
        return httpx.Response(200, json={
            "data": [{"embedding": [0.5] * EMBEDDING_DIM}],
        })

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        result = await embed_text("text", model="text-embedding-3-large")

    assert captured["model"] == "text-embedding-3-large"
    assert len(result) == EMBEDDING_DIM


# ===================== embed_text — 无 API key =====================

async def test_embed_text_no_api_key_returns_zeros() -> None:
    """无 API key 时返回零向量。"""
    with patch.object(settings, "openai_api_key", ""):
        result = await embed_text("hello")

    assert result == _zero_vector()
    assert len(result) == EMBEDDING_DIM
    assert all(v == 0.0 for v in result)


async def test_embed_text_empty_api_key_returns_zeros() -> None:
    """API key 为空字符串时返回零向量。"""
    with patch.object(settings, "openai_api_key", ""):
        result = await embed_text("test text")
    assert result == [0.0] * EMBEDDING_DIM


# ===================== embed_text — API 错误 =====================

async def test_embed_text_api_error_returns_zeros() -> None:
    """API 错误时返回零向量。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        result = await embed_text("hello")

    assert result == _zero_vector()
    assert all(v == 0.0 for v in result)


async def test_embed_text_connect_error_returns_zeros() -> None:
    """连接错误时返回零向量。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        result = await embed_text("hello")

    assert result == [0.0] * EMBEDDING_DIM


async def test_embed_text_malformed_response_returns_zeros() -> None:
    """响应缺少 embedding 字段时返回零向量（KeyError/IndexError 回退）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        # 缺少 data 字段
        return httpx.Response(200, json={"unexpected": "format"})

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        result = await embed_text("hello")

    assert result == [0.0] * EMBEDDING_DIM


async def test_embed_text_empty_data_array_returns_zeros() -> None:
    """data 数组为空时返回零向量（IndexError 回退）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        result = await embed_text("hello")

    assert result == [0.0] * EMBEDDING_DIM


# ===================== embed_batch =====================

async def test_embed_batch() -> None:
    """批量嵌入。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        body = __import__("json").loads(request.content)
        # 按输入顺序为每条文本返回不同向量
        inputs = body["input"]
        data = [
            {"embedding": [float(i)] * EMBEDDING_DIM}
            for i in range(len(inputs))
        ]
        return httpx.Response(200, json={"data": data})

    texts = ["first", "second", "third"]
    with _patch_embedder_http(handler), \
         patch.object(settings, "openai_api_key", "test-key"):
        results = await embed_batch(texts)

    assert len(results) == 3
    assert call_count["n"] == 1  # 单次批量调用
    assert results[0][0] == 0.0
    assert results[1][0] == 1.0
    assert results[2][0] == 2.0
    assert all(len(r) == EMBEDDING_DIM for r in results)


async def test_embed_batch_empty() -> None:
    """空列表批量嵌入返回空列表。"""
    with patch.object(settings, "openai_api_key", "test-key"):
        results = await embed_batch([])
    assert results == []


async def test_embed_batch_with_no_api_key() -> None:
    """无 API key 时批量嵌入全部返回零向量。"""
    with patch.object(settings, "openai_api_key", ""):
        results = await embed_batch(["a", "b"])

    assert len(results) == 2
    assert all(r == [0.0] * EMBEDDING_DIM for r in results)


# ===================== 维度 =====================

def test_embedding_dimension() -> None:
    """向量维度为 1536。"""
    vec = _zero_vector()
    assert len(vec) == EMBEDDING_DIM
    assert EMBEDDING_DIM == 1536
    assert all(v == 0.0 for v in vec)


def test_zero_vector_is_immutable_in_practice() -> None:
    """零向量每次调用返回新列表。"""
    v1 = _zero_vector()
    v2 = _zero_vector()
    assert v1 == v2
    assert v1 is not v2  # 不同实例
    v1[0] = 1.0
    assert v2[0] == 0.0  # 修改 v1 不影响 v2


# ===================== P1-7：向量维度解耦 =====================

def test_embedding_model_registry_lists_known_models() -> None:
    """P1-7：注册表登记已知模型及其维度。"""
    from app.domains.knowledge.embedder import EMBEDDING_MODEL_REGISTRY

    assert "text-embedding-3-small" in EMBEDDING_MODEL_REGISTRY
    assert EMBEDDING_MODEL_REGISTRY["text-embedding-3-small"] == 1536
    assert EMBEDDING_MODEL_REGISTRY["text-embedding-3-large"] == 3072


def test_get_embedding_dim_returns_registry_value() -> None:
    """P1-7：get_embedding_dim 返回注册表维度。"""
    from app.domains.knowledge.embedder import get_embedding_dim

    assert get_embedding_dim("text-embedding-3-small") == 1536
    assert get_embedding_dim("text-embedding-3-large") == 3072


def test_get_embedding_dim_raises_for_unknown_model() -> None:
    """P1-7：未登记模型抛 KeyError（调用方应在校验阶段拦截）。"""
    from app.domains.knowledge.embedder import get_embedding_dim

    with pytest.raises(KeyError):
        get_embedding_dim("unknown-model")


def test_zero_vector_respects_dim_parameter() -> None:
    """P1-7：_zero_vector(dim) 按传入维度生成，不再硬编码 1536。"""
    assert len(_zero_vector(3072)) == 3072
    assert len(_zero_vector(768)) == 768
    # 默认值仍为 1536（向后兼容）
    assert len(_zero_vector()) == 1536
