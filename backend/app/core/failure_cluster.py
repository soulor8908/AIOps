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
- C6：可选 SQLite DLQ 持久化——``add`` 先入 DLQ 再 embed，embedder 失败时
  记录留在 DLQ（``embedded=0``）供 ``replay_dlq`` 重试。进程重启后 DLQ 保留
  未 embed 的记录，避免丢失。DLQ 关闭时退化为纯内存（与历史行为一致）。
  embedder 失败时该条记录用零向量，聚类时与任意向量距离=1.0（自成一簇）。
"""

from __future__ import annotations

import json
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


class FailureDLQ:
    """C6：SQLite 持久化死信队列——保存失败记录供 embed 重试与重启恢复。

    表结构：
    - ``id`` TEXT PK：与 FailureRecord.id 一致
    - ``message`` TEXT：原始 error message
    - ``metadata`` TEXT：JSON 序列化的 metadata
    - ``timestamp`` REAL：记录时间戳
    - ``embedded`` INTEGER：0=未 embed（embedder 失败），1=已 embed
    - ``embedding`` TEXT：JSON 序列化的 embedding 向量（已 embed 时填充）

    设计：
    - 轻量 ``aiosqlite`` 直连，不经 SQLAlchemy（DLQ 是高频小写入，ORM 开销不必要）。
    - ``:memory:`` 模式用于单测；文件路径用于生产持久化。
    - DLQ 是 source of truth，内存缓冲是 hot working set——缓冲溢出裁剪不丢 DLQ 数据。
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn: Any = None  # aiosqlite.Connection，懒初始化

    async def _ensure(self) -> Any:
        """懒初始化连接 + 建表。"""
        if self._conn is not None:
            return self._conn
        import aiosqlite

        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_dlq (
                id TEXT PRIMARY KEY,
                message TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                timestamp REAL NOT NULL,
                embedded INTEGER NOT NULL DEFAULT 0,
                embedding TEXT
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_failure_dlq_embedded ON failure_dlq(embedded)"
        )
        await self._conn.commit()
        return self._conn

    async def enqueue(
        self,
        record_id: uuid.UUID,
        message: str,
        metadata: dict[str, Any],
        timestamp: float,
    ) -> None:
        """入队一条未 embed 的记录。"""
        conn = await self._ensure()
        await conn.execute(
            "INSERT OR REPLACE INTO failure_dlq (id, message, metadata, timestamp, embedded, embedding) "
            "VALUES (?, ?, ?, ?, 0, NULL)",
            (str(record_id), message, json.dumps(metadata, ensure_ascii=False), timestamp),
        )
        await conn.commit()

    async def mark_embedded(
        self, record_id: uuid.UUID, embedding: list[float]
    ) -> None:
        """标记记录已 embed，存入 embedding 向量。"""
        conn = await self._ensure()
        await conn.execute(
            "UPDATE failure_dlq SET embedded = 1, embedding = ? WHERE id = ?",
            (json.dumps(embedding), str(record_id)),
        )
        await conn.commit()

    async def get_unembedded(self, limit: int = 100) -> list[dict[str, Any]]:
        """取未 embed 的记录（供 replay_dlq 重试）。按时间升序。"""
        conn = await self._ensure()
        cursor = await conn.execute(
            "SELECT id, message, metadata, timestamp FROM failure_dlq "
            "WHERE embedded = 0 ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": uuid.UUID(row[0]),
                "message": row[1],
                "metadata": json.loads(row[2]),
                "timestamp": row[3],
            }
            for row in rows
        ]

    async def count(self) -> int:
        """DLQ 总记录数。"""
        conn = await self._ensure()
        cursor = await conn.execute("SELECT COUNT(*) FROM failure_dlq")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def count_unembedded(self) -> int:
        """未 embed 的记录数。"""
        conn = await self._ensure()
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM failure_dlq WHERE embedded = 0"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def close(self) -> None:
        """关闭连接。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


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

    C6：可选传入 ``dlq: FailureDLQ`` 启用 SQLite 持久化。启用后 ``add`` 先入
    DLQ 再 embed，embedder 失败时记录留在 DLQ（``embedded=0``）供
    ``replay_dlq`` 重试。``dlq=None`` 时退化为纯内存（与历史行为一致）。
    """

    def __init__(
        self,
        distance_threshold: float = _DEFAULT_DISTANCE_THRESHOLD,
        buffer_limit: int = _DEFAULT_BUFFER_LIMIT,
        dlq: FailureDLQ | None = None,
    ) -> None:
        self._threshold = max(0.0, distance_threshold)
        self._buffer_limit = max(1, buffer_limit)
        self._records: list[FailureRecord] = []
        self._dlq = dlq

    async def add(
        self, message: str, metadata: dict[str, Any] | None = None
    ) -> FailureRecord:
        """向量化 message 并存入缓冲区。embedder 失败用零向量（不抛错）。

        C6：若启用 DLQ，先入队再 embed。embed 成功后标记 DLQ 记录为 embedded；
        embed 失败时 DLQ 记录保持 ``embedded=0``，供 ``replay_dlq`` 重试。
        """
        record = FailureRecord(
            id=uuid.uuid4(),
            message=message,
            embedding=[],
            metadata=metadata or {},
        )
        # C6：先入 DLQ（持久化，embed 前保全记录）
        if self._dlq is not None:
            try:
                await self._dlq.enqueue(
                    record.id, message, record.metadata, record.timestamp
                )
            except Exception:  # noqa: BLE001
                logger.debug("DLQ enqueue 失败，降级为纯内存", exc_info=True)
        # 向量化（embedder 失败用零向量）
        embedding = await embed_text(message)
        record.embedding = embedding
        # C6：embed 成功后标记 DLQ 记录
        if self._dlq is not None:
            try:
                await self._dlq.mark_embedded(record.id, embedding)
            except Exception:  # noqa: BLE001
                logger.debug("DLQ mark_embedded 失败", exc_info=True)
        self._records.append(record)
        # 缓冲区裁剪：保留最近 N 条（DLQ 保留全量，不丢数据）
        if len(self._records) > self._buffer_limit:
            self._records = self._records[-self._buffer_limit :]
        return record

    async def replay_dlq(self, limit: int = 100) -> int:
        """C6：重试 DLQ 中未 embed 的记录。

        取 ``limit`` 条 ``embedded=0`` 的记录，重新 embed 并加入内存缓冲。
        embed 成功后标记为 ``embedded=1``。返回成功 replay 的记录数。

        适用场景：embedder API 短暂故障后恢复，或进程重启后从 DLQ 恢复。
        """
        if self._dlq is None:
            return 0
        pending = await self._dlq.get_unembedded(limit=limit)
        replayed = 0
        for item in pending:
            try:
                embedding = await embed_text(item["message"])
            except Exception:  # noqa: BLE001
                logger.debug(
                    "replay_dlq: embed 仍失败，跳过 id=%s", item["id"], exc_info=True
                )
                continue
            record = FailureRecord(
                id=item["id"],
                message=item["message"],
                embedding=embedding,
                metadata=item["metadata"],
                timestamp=item["timestamp"],
            )
            self._records.append(record)
            await self._dlq.mark_embedded(record.id, embedding)
            replayed += 1
        # 裁剪缓冲区
        if len(self._records) > self._buffer_limit:
            self._records = self._records[-self._buffer_limit :]
        if replayed > 0:
            logger.info("replay_dlq: 成功重试 %d 条记录", replayed)
        return replayed

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
    C6：若 ``agent_failure_cluster_dlq_path`` 非空，构造 FailureDLQ 注入。
    """
    global _singleton
    if _singleton is None:
        from app.core.config import settings

        dlq: FailureDLQ | None = None
        if settings.agent_failure_cluster_dlq_path:
            dlq = FailureDLQ(settings.agent_failure_cluster_dlq_path)
        _singleton = FailureClusterer(
            distance_threshold=settings.agent_failure_cluster_distance_threshold,
            dlq=dlq,
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
    "FailureDLQ",
    "FailureRecord",
    "cosine_distance",
]
