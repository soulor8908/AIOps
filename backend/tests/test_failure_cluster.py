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
    FailureDLQ,
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


# ===================== 6. C6 SQLite DLQ =====================


async def test_dlq_enqueue_and_count() -> None:
    """C6：DLQ enqueue 后 count 增长，未 embed 计数同步。"""
    dlq = FailureDLQ(":memory:")
    try:
        assert await dlq.count() == 0
        rid = uuid.uuid4()
        await dlq.enqueue(rid, "error msg", {"tool": "t"}, 1000.0)
        assert await dlq.count() == 1
        assert await dlq.count_unembedded() == 1
    finally:
        await dlq.close()


async def test_dlq_mark_embedded_reduces_unembedded() -> None:
    """C6：mark_embedded 后未 embed 计数减少。"""
    dlq = FailureDLQ(":memory:")
    try:
        rid = uuid.uuid4()
        await dlq.enqueue(rid, "error", {}, 1000.0)
        assert await dlq.count_unembedded() == 1

        await dlq.mark_embedded(rid, [0.1, 0.2, 0.3])
        assert await dlq.count_unembedded() == 0
        assert await dlq.count() == 1  # 总数不变
    finally:
        await dlq.close()


async def test_dlq_get_unembedded_returns_oldest_first() -> None:
    """C6：get_unembedded 按时间升序返回。"""
    dlq = FailureDLQ(":memory:")
    try:
        await dlq.enqueue(uuid.uuid4(), "newer", {}, 2000.0)
        await dlq.enqueue(uuid.uuid4(), "older", {}, 1000.0)
        pending = await dlq.get_unembedded()
        assert len(pending) == 2
        assert pending[0]["message"] == "older"
        assert pending[1]["message"] == "newer"
    finally:
        await dlq.close()


async def test_clusterer_with_dlq_persists_records() -> None:
    """C6：add 写入 DLQ，embed 成功后标记 embedded。"""
    dlq = FailureDLQ(":memory:")
    clusterer = FailureClusterer(dlq=dlq)
    try:
        await clusterer.add("tool timeout", {"tool": "search"})
        # DLQ 有 1 条记录，已 embed
        assert await dlq.count() == 1
        assert await dlq.count_unembedded() == 0
        # 内存缓冲也有 1 条
        assert clusterer.record_count == 1
    finally:
        await dlq.close()


async def test_clusterer_without_dlq_backward_compatible() -> None:
    """C6：dlq=None 时退化为纯内存，行为与历史一致。"""
    clusterer = FailureClusterer()  # dlq 默认 None
    record = await clusterer.add("error msg", {"k": "v"})
    assert record.message == "error msg"
    assert clusterer.record_count == 1
    # replay_dlq 在无 DLQ 时返回 0
    assert await clusterer.replay_dlq() == 0


async def test_replay_dlq_retries_unembedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6：DLQ 中有 unembedded 记录 → replay embed 成功 → 加入缓冲 + 标记。

    模拟场景：进程崩溃前 embed 未完成，DLQ 留下 embedded=0 的记录。
    重启后 replay_dlq 重新 embed，成功后加入内存缓冲。
    """
    import app.core.failure_cluster as fc_mod

    dlq = FailureDLQ(":memory:")
    clusterer = FailureClusterer(dlq=dlq)
    try:
        # 直接入队一条 unembedded 记录（模拟 add 崩溃后遗留）
        rid = uuid.uuid4()
        await dlq.enqueue(rid, "crash leftover error", {"tool": "x"}, 1000.0)
        assert await dlq.count_unembedded() == 1

        # mock embed_text 返回有效向量
        async def _ok_embed(text: str) -> list[float]:
            return [0.5, 0.5]

        monkeypatch.setattr(fc_mod, "embed_text", _ok_embed)

        replayed = await clusterer.replay_dlq()
        assert replayed == 1
        assert await dlq.count_unembedded() == 0
        assert clusterer.record_count == 1
    finally:
        await dlq.close()


async def test_replay_dlq_skips_still_failing_embeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6：replay 时 embed 仍失败 → 跳过，记录留在 DLQ 待下次重试。"""
    import app.core.failure_cluster as fc_mod

    dlq = FailureDLQ(":memory:")
    clusterer = FailureClusterer(dlq=dlq)
    try:
        await dlq.enqueue(uuid.uuid4(), "persistently failing", {}, 1000.0)

        async def _always_fail(text: str) -> list[float]:
            raise RuntimeError("embed still down")

        monkeypatch.setattr(fc_mod, "embed_text", _always_fail)

        replayed = await clusterer.replay_dlq()
        assert replayed == 0
        # 记录仍在 DLQ，embedded=0
        assert await dlq.count_unembedded() == 1
        assert clusterer.record_count == 0
    finally:
        await dlq.close()


async def test_get_failure_clusterer_with_dlq_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6：settings 配置 dlq_path 时单例注入 DLQ。"""
    import app.core.failure_cluster as fc_mod
    from app.core.config import settings

    # 重置单例
    monkeypatch.setattr(fc_mod, "_singleton", None)
    monkeypatch.setattr(settings, "agent_failure_cluster_dlq_path", ":memory:")

    clusterer = fc_mod.get_failure_clusterer()
    assert clusterer._dlq is not None
    assert isinstance(clusterer._dlq, FailureDLQ)

    # 清理：关闭 DLQ 连接 + 重置单例
    if clusterer._dlq is not None:
        await clusterer._dlq.close()
    monkeypatch.setattr(fc_mod, "_singleton", None)
    monkeypatch.setattr(settings, "agent_failure_cluster_dlq_path", "")


async def test_get_failure_clusterer_without_dlq_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6：settings dlq_path 为空时单例无 DLQ（纯内存）。"""
    import app.core.failure_cluster as fc_mod
    from app.core.config import settings

    monkeypatch.setattr(fc_mod, "_singleton", None)
    monkeypatch.setattr(settings, "agent_failure_cluster_dlq_path", "")

    clusterer = fc_mod.get_failure_clusterer()
    assert clusterer._dlq is None

    monkeypatch.setattr(fc_mod, "_singleton", None)
