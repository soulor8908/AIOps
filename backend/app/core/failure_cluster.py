"""失败模式聚类（P2-8）— error message 向量化 + 语义聚类。

设计要点：
- ``FailureRecord``：单条失败记录（message + embedding + metadata + ts）。
- ``FailureClusterer``：
  - ``add``：用 embedder 向量化 error message 后存入内存缓冲。
  - ``cluster``：批量单链接聚类（single-linkage agglomerative）——
    计算 pairwise 余弦距离矩阵（numpy），用 Union-Find 合并距离 < 阈值的样本。
    返回 ``FailureCluster`` 列表（代表消息 + 计数 + 样本 + 质心）。
- 无 HDBSCAN/sklearn 依赖——单链接 + 余弦阈值在 error message 场景
  （语义相近 = 同类错误）效果接近 HDBSCAN，且零额外依赖。
- 纯内存实现（生产应持久化到 DB，定期 re-cluster）。embedder 失败时
  该条记录用零向量，聚类时与任意向量距离=1.0（自成一簇或归入其他零向量簇）。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.domains.knowledge.embedder import embed_text

logger = logging.getLogger("app.core.failure_cluster")

# 默认聚类距离阈值（余弦距离 ∈ [0,2]，0=完全相同，1=正交，2=相反）。
# 0.3 对应 cosine_similarity ≈ 0.7，语义相近的错误归为同簇。
_DEFAULT_DISTANCE_THRESHOLD = 0.3
# 默认单簇最大样本展示数（避免 cluster.samples 过长）
_MAX_SAMPLES_PER_CLUSTER = 5
# 默认缓冲区上限（超出后 cluster() 自动触发裁剪，保留最近 N 条）
_DEFAULT_BUFFER_LIMIT = 10000


@dataclass(slots=True)
class FailureRecord:
    """单条失败记录。"""

    id: uuid.UUID
    message: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class FailureCluster:
    """聚类结果。"""

    cluster_id: int
    representative_message: str
    count: int
    centroid: list[float]
    samples: list[FailureRecord]


class FailureClusterer:
    """失败模式聚类器。

    用法：
        clusterer = FailureClusterer()
        await clusterer.add("tool search timeout", {"tool": "search"})
        await clusterer.add("tool search timed out", {"tool": "search"})
        clusters = clusterer.cluster()
        # clusters[0].count == 2, representative_message 为首条
    """

    def __init__(
        self,
        distance_threshold: float = _DEFAULT_DISTANCE_THRESHOLD,
        buffer_limit: int = _DEFAULT_BUFFER_LIMIT,
    ) -> None:
        self._threshold = max(0.0, distance_threshold)
        self._buffer_limit = max(1, buffer_limit)
        self._records: list[FailureRecord] = []

    async def add(
        self, message: str, metadata: dict[str, Any] | None = None
    ) -> FailureRecord:
        """向量化 message 并存入缓冲区。embedder 失败用零向量（不抛错）。"""
        embedding = await embed_text(message)
        record = FailureRecord(
            id=uuid.uuid4(),
            message=message,
            embedding=embedding,
            metadata=metadata or {},
        )
        self._records.append(record)
        # 缓冲区裁剪：保留最近 N 条
        if len(self._records) > self._buffer_limit:
            self._records = self._records[-self._buffer_limit :]
        return record

    def cluster(
        self, distance_threshold: float | None = None
    ) -> list[FailureCluster]:
        """批量聚类。返回按 count 降序的簇列表。

        单链接聚类：pairwise 余弦距离 < 阈值 → Union-Find 合并。
        无记录/全零向量时返回空列表。
        """
        threshold = (
            distance_threshold if distance_threshold is not None else self._threshold
        )
        records = self._records
        n = len(records)
        if n == 0:
            return []
        # 构造 embedding 矩阵
        matrix = np.array([r.embedding for r in records], dtype=np.float32)
        # 归一化后点积 = cosine 相似度，1 - 相似度 = 余弦距离
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        # 避免除零（零向量 norm=0 → 距离设为 1.0 即正交）
        safe_norms = np.where(norms == 0, 1.0, norms)
        normalized = matrix / safe_norms
        sim = normalized @ normalized.T
        dist = 1.0 - sim
        # Union-Find
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # 合并距离 < 阈值的样本对
        for i in range(n):
            for j in range(i + 1, n):
                if dist[i][j] < threshold:
                    union(i, j)
        # 按 root 分组
        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(i)
        # 构造 FailureCluster
        clusters: list[FailureCluster] = []
        for cid, (_root, indices) in enumerate(groups.items()):
            members = [records[i] for i in indices]
            # 质心 = 成员 embedding 均值
            member_emb = np.array([r.embedding for r in members], dtype=np.float32)
            centroid = member_emb.mean(axis=0).tolist()
            # 代表消息 = 首条（按时间序）
            representative = members[0].message
            clusters.append(
                FailureCluster(
                    cluster_id=cid,
                    representative_message=representative,
                    count=len(members),
                    centroid=centroid,
                    samples=members[:_MAX_SAMPLES_PER_CLUSTER],
                )
            )
        # 按 count 降序
        clusters.sort(key=lambda c: -c.count)
        return clusters

    @property
    def record_count(self) -> int:
        """当前缓冲区记录数。"""
        return len(self._records)

    def clear(self) -> None:
        """清空缓冲区。"""
        self._records.clear()


# 进程级单例（惰性构造）。生产多实例应换共享存储。
_singleton: FailureClusterer | None = None


def get_failure_clusterer() -> FailureClusterer:
    """获取进程级 FailureClusterer 单例。

    延迟 import settings 避免循环依赖。threshold/buffer 用 config 默认值。
    """
    global _singleton
    if _singleton is None:
        from app.core.config import settings

        _singleton = FailureClusterer(
            distance_threshold=settings.agent_failure_cluster_distance_threshold,
        )
    return _singleton


def cosine_distance(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦距离（纯函数，供测试直接调用）。

    1 - cosine_similarity。零向量返回 1.0（正交）。
    """
    arr_a = np.array(a, dtype=np.float32)
    arr_b = np.array(b, dtype=np.float32)
    na = np.linalg.norm(arr_a)
    nb = np.linalg.norm(arr_b)
    if na == 0 or nb == 0:
        return 1.0
    return float(1.0 - np.dot(arr_a, arr_b) / (na * nb))


__all__ = [
    "FailureCluster",
    "FailureClusterer",
    "FailureRecord",
    "cosine_distance",
]
