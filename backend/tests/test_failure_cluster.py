"""失败模式聚类（P2-8）测试 — error message 向量化 + 语义聚类。

覆盖：
1. **cosine_distance** 纯函数
2. **FailureClusterer.add**：向量化存储 + 缓冲区裁剪
3. **FailureClusterer.cluster**：相似错误归同簇、不同错误分簇、空列表、零向量
4. **API**：/agents/failure-clusters 端点（admin 权限）
5. **executor 集成**：工具失败时记录到 clusterer（mock）
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.failure_cluster import (
    FailureCluster,
    FailureClusterer,
    FailureRecord,
    cosine_distance,
)
from app.main import app

# ===================== 1. cosine_distance =====================


def test_cosine_distance_identical_vectors_is_zero() -> None:
    v = [1.0, 0.0, 0.0]
    assert cosine_distance(v, v) == pytest.approx(0.0, abs=1e-6)


def test_cosine_distance_orthogonal_is_one() -> None:
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0, abs=1e-6)


def test_cosine_distance_opposite_is_two() -> None:
    assert cosine_distance([1.0], [-1.0]) == pytest.approx(2.0, abs=1e-6)


def test_cosine_distance_zero_vector_returns_one() -> None:
    assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_distance([1.0, 0.0], [0.0, 0.0]) == 1.0


# ===================== 2. FailureClusterer.add =====================


async def test_add_stores_record_with_embedding() -> None:
    """add 向量化 message 后存入缓冲区。embedder 无 API key → 零向量。"""
    clusterer = FailureClusterer()
    record = await clusterer.add("tool search timeout", {"tool": "search"})
    assert isinstance(record, FailureRecord)
    assert record.message == "tool search timeout"
    assert record.metadata == {"tool": "search"}
    assert len(record.embedding) == 1536  # text-embedding-3-small 维度
    assert clusterer.record_count == 1


async def test_add_trims_buffer_when_exceeds_limit() -> None:
    """缓冲区超限时保留最近 N 条。"""
    clusterer = FailureClusterer(buffer_limit=3)
    for i in range(5):
        await clusterer.add(f"error {i}")
    assert clusterer.record_count == 3
    # 保留最近 3 条（error 2/3/4）
    clusters = clusterer.cluster()
    messages = {s.message for c in clusters for s in c.samples}
    assert "error 2" in messages
    assert "error 0" not in messages


# ===================== 3. FailureClusterer.cluster =====================


async def test_cluster_groups_similar_errors() -> None:
    """语义相近的 error message 归为同簇。"""
    clusterer = FailureClusterer(distance_threshold=0.5)
    # 用 mock embedding 构造可控相似度（同向 = 完全相同 = 距离 0）
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message="timeout error", embedding=[1.0, 0.0]),
        FailureRecord(id=uuid.uuid4(), message="timed out", embedding=[1.0, 0.0]),
        FailureRecord(id=uuid.uuid4(), message="auth failed", embedding=[0.0, 1.0]),
    ]
    clusters = clusterer.cluster()
    # 两个簇：timeout 类（2 条）+ auth 类（1 条）
    assert len(clusters) == 2
    # 按 count 降序，timeout 类在前
    assert clusters[0].count == 2
    assert clusters[1].count == 1
    assert clusters[0].cluster_id == 0


async def test_cluster_separates_different_errors() -> None:
    """完全不同的 error message 分到不同簇。"""
    clusterer = FailureClusterer(distance_threshold=0.3)
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message="a", embedding=[1.0, 0.0]),
        FailureRecord(id=uuid.uuid4(), message="b", embedding=[0.0, 1.0]),
        FailureRecord(id=uuid.uuid4(), message="c", embedding=[0.0, 0.0]),  # 零向量
    ]
    clusters = clusterer.cluster()
    # 三簇（正交 + 零向量与任意距离=1.0 > 0.3）
    assert len(clusters) == 3


def test_cluster_empty_returns_empty_list() -> None:
    clusterer = FailureClusterer()
    assert clusterer.cluster() == []


async def test_cluster_threshold_controls_granularity() -> None:
    """阈值越大，簇越粗（合并越多）。"""
    clusterer = FailureClusterer()
    # cosine_distance([1,0],[1,1]) ≈ 0.293
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message="a", embedding=[1.0, 0.0]),
        FailureRecord(id=uuid.uuid4(), message="b", embedding=[1.0, 1.0]),
    ]
    # 阈值 0.2 → 不合并 → 2 簇
    assert len(clusterer.cluster(distance_threshold=0.2)) == 2
    # 阈值 0.5 → 合并 → 1 簇
    assert len(clusterer.cluster(distance_threshold=0.5)) == 1


async def test_cluster_representative_is_first_member() -> None:
    """代表消息为首条（按添加顺序）。"""
    clusterer = FailureClusterer()
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message="first", embedding=[1.0, 0.0]),
        FailureRecord(id=uuid.uuid4(), message="second", embedding=[1.0, 0.0]),
    ]
    clusters = clusterer.cluster()
    assert clusters[0].representative_message == "first"


async def test_cluster_samples_capped() -> None:
    """samples 最多展示 _MAX_SAMPLES_PER_CLUSTER 条。"""
    clusterer = FailureClusterer()
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message=f"m{i}", embedding=[1.0, 0.0])
        for i in range(10)
    ]
    clusters = clusterer.cluster()
    assert len(clusters) == 1
    assert clusters[0].count == 10
    from app.core.failure_cluster import _MAX_SAMPLES_PER_CLUSTER

    assert len(clusters[0].samples) == _MAX_SAMPLES_PER_CLUSTER


def test_clear_empties_buffer() -> None:
    clusterer = FailureClusterer()
    clusterer._records = [
        FailureRecord(id=uuid.uuid4(), message="x", embedding=[1.0])
    ]
    clusterer.clear()
    assert clusterer.record_count == 0


# ===================== 4. API =====================


def test_failure_clusters_endpoint_requires_admin(
    user_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 admin 用户访问 /agents/failure-clusters 返回 403。"""
    resp = user_client.get("/api/v1/agents/failure-clusters")
    assert resp.status_code == 403


def test_failure_clusters_endpoint_returns_clusters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admin 用户可查看聚类结果。"""
    # mock admin 权限
    from app.core.deps import get_current_admin

    admin_user = MagicMock()
    admin_user.is_admin = True
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    try:
        # mock clusterer 返回固定簇
        from app.core import failure_cluster as fc_mod

        mock_clusterer = MagicMock(spec=FailureClusterer)
        mock_clusterer.cluster.return_value = [
            FailureCluster(
                cluster_id=0,
                representative_message="timeout error",
                count=3,
                centroid=[0.1] * 1536,
                samples=[
                    FailureRecord(
                        id=uuid.uuid4(),
                        message="timeout error",
                        embedding=[0.1] * 1536,
                        metadata={"tool": "search"},
                    )
                ],
            )
        ]
        monkeypatch.setattr(fc_mod, "get_failure_clusterer", lambda: mock_clusterer)

        resp = client.get("/api/v1/agents/failure-clusters")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["representative_message"] == "timeout error"
        assert data[0]["count"] == 3
        assert data[0]["samples"][0]["message"] == "timeout error"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)


# ===================== 5. executor 集成 =====================


async def test_record_failure_safely_disabled_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_failure_clustering_enabled=False 时不记录。"""
    from app.core.config import settings
    from app.domains.agents.executor import _record_failure_safely

    monkeypatch.setattr(settings, "agent_failure_clustering_enabled", False)
    # 不应抛异常，也不应调 clusterer
    _record_failure_safely("err", {})
    # 无 task 被创建（无法直接验证，但确保不抛错即可）


async def test_record_failure_safely_enabled_creates_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用时创建 asyncio.create_task 调用 clusterer.add。"""
    from app.core.config import settings
    from app.domains.agents.executor import _record_failure_safely

    monkeypatch.setattr(settings, "agent_failure_clustering_enabled", True)

    mock_clusterer = MagicMock(spec=FailureClusterer)
    add_called = asyncio.Event()

    async def _fake_add(msg: str, meta: dict) -> Any:
        add_called.set()
        return FailureRecord(
            id=uuid.uuid4(), message=msg, embedding=[0.0] * 1536, metadata=meta
        )

    mock_clusterer.add = _fake_add
    monkeypatch.setattr(
        "app.core.failure_cluster.get_failure_clusterer",
        lambda: mock_clusterer,
    )

    _record_failure_safely("tool error", {"tool": "t"})
    # 等待 task 完成
    await asyncio.wait_for(add_called.wait(), timeout=1.0)
