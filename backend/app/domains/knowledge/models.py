"""Knowledge Base — ORM + Pydantic schemas。

ORM: KnowledgeBase, Document, Chunk（embedding: VECTOR(1536)）
Schema: KnowledgeBaseCreate / KnowledgeBaseOut / DocumentOut /
        SearchResult / SearchQuery
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import TSVECTOR as TSVector
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

EMBEDDING_DIM = 1536


# ===================== ORM =====================

class KnowledgeBase(Base):
    """知识库。每个 KB 独立配置分块策略与 embedding 模型。

    P4-1：``owner_id`` 字段实现资源隔离——非 admin 仅能访问自己的 KB,
    admin 可访问全部。旧数据 owner_id 为 NULL,仅 admin 可见(兼容)。
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(
        String(64), nullable=False, default="text-embedding-3-small"
    )
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=800)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # P4-1：资源隔离。nullable 兼容旧数据(无 owner 的 KB 仅 admin 可见)。
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    documents: Mapped[list[Document]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )


class Document(Base):
    """知识库文档。status: pending/processing/ready/failed。"""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_documents_status", "status"),)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")


class Chunk(Base):
    """文档分块。embedding 维度 1536 对齐 OpenAI text-embedding-3-small。"""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    # BM25 全文检索列：PG 上由 ingest 时 func.to_tsvector('simple', content) 写入；
    # SQLite 上由 conftest 渲染为 TEXT（仅存储，全文检索降级为 LIKE）。
    search_vector: Mapped[Any] = mapped_column(TSVector, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # HNSW 向量索引，余弦距离；PG 专属，SQLite 上降级为普通索引（dialect kwargs 被忽略）
        Index(
            "idx_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # GIN 倒排索引，加速 @@ tsquery 匹配；PG 专属，SQLite 上降级为普通索引
        Index(
            "idx_knowledge_chunks_search",
            "search_vector",
            postgresql_using="gin",
        ),
    )


# ===================== Schemas =====================

class KnowledgeBaseCreate(BaseModel):
    """创建知识库入参。"""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    embedding_model: str = "text-embedding-3-small"
    chunk_size: int = Field(default=800, ge=100, le=8000)
    chunk_overlap: int = Field(default=100, ge=0, le=500)


class KnowledgeBaseOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str | None = None
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    owner_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class DocumentOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    title: str
    source_uri: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    chunk_count: int
    status: str
    created_at: datetime
    updated_at: datetime


class SearchQuery(BaseModel):
    """向量检索请求。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class SearchResult(BaseModel):
    """单条检索结果。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class RAGQuery(BaseModel):
    """RAG 查询请求（检索 + LLM 生成）。

    ``rerank=True`` 时启用 LLM reranker（单次 LLM 调用对候选打分重排），
    默认关闭以避免额外的 LLM 成本与延迟；hybrid search + RRF 融合始终启用。
    """

    question: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    rerank: bool = False
