"""Knowledge Base — FastAPI 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import ValidationError
from app.domains.knowledge import service
from app.domains.knowledge.models import (
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    RAGQuery,
    SearchQuery,
    SearchResult,
)
from app.domains.knowledge.service import MAX_DOC_BYTES

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])

# security.spec.md§7.2 — 文件上传 content-type 白名单。
# 禁止 octet-stream、可执行类、脚本类 MIME。
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"text/plain", "text/markdown", "application/pdf"}
)
# Content-Length 预检阈值：50MB 文档 + 1MB multipart 开销余量（security.spec.md§7.1）。
_MAX_CONTENT_LENGTH = MAX_DOC_BYTES + 1024 * 1024


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[KnowledgeBaseOut]:
    kbs = await service.list_kbs(session, limit=limit, offset=offset)
    return [KnowledgeBaseOut.model_validate(k) for k in kbs]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    payload: KnowledgeBaseCreate, session: AsyncSession = Depends(get_session)
) -> KnowledgeBaseOut:
    kb = await service.create_kb(session, payload)
    return KnowledgeBaseOut.model_validate(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_kb(
    kb_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> KnowledgeBaseOut:
    kb = await service.get_kb(session, kb_id)
    return KnowledgeBaseOut.model_validate(kb)


@router.post(
    "/{kb_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: uuid.UUID,
    request: Request,
    title: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    # security.spec.md§7.1 — 先检 Content-Length，超限直接拒绝（防大文件耗尽内存）。
    content_length = int(request.headers.get("content-length", "0"))
    if content_length > _MAX_CONTENT_LENGTH:
        raise ValidationError(
            f"文件超 {MAX_DOC_BYTES // 1024 // 1024}MB 上限"
            f"（Content-Length: {content_length} bytes）"
        )
    # security.spec.md§7.2 — content-type 白名单校验。
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            f"不支持的文件类型 '{file.content_type}'，"
            f"仅允许: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )
    content = (await file.read()).decode("utf-8", errors="replace")
    if not content.strip():
        raise ValidationError("文档内容为空")
    # security.spec.md§7.4 — 文件名 UUID 重命名，禁止保留用户原始文件名（防目录穿越）。
    source_uri = str(uuid.uuid4())
    doc = await service.upload_document(
        session,
        kb_id,
        title=title,
        content=content,
        mime_type=file.content_type,
        source_uri=source_uri,
    )
    return DocumentOut.model_validate(doc)


@router.post("/{kb_id}/search", response_model=list[SearchResult])
async def search_kb(
    kb_id: uuid.UUID,
    payload: SearchQuery,
    session: AsyncSession = Depends(get_session),
) -> list[SearchResult]:
    return await service.search_kb(session, kb_id, payload)


@router.post("/{kb_id}/rag")
async def rag_query(
    kb_id: uuid.UUID,
    payload: RAGQuery,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    return await service.rag_query(session, kb_id, payload)
