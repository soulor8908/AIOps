"""Knowledge Base — FastAPI 路由。"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.core.exceptions import ValidationError
from app.domains.auth.models import User
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

logger = logging.getLogger("app.audit.knowledge")

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
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeBaseOut]:
    kbs = await service.list_kbs(session, limit=limit, offset=offset)
    return [KnowledgeBaseOut.model_validate(k) for k in kbs]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    payload: KnowledgeBaseCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> KnowledgeBaseOut:
    kb = await service.create_kb(session, payload)
    return KnowledgeBaseOut.model_validate(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_kb(
    kb_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
) -> DocumentOut:
    # security.spec.md§7.1 — 先检 Content-Length，超限直接拒绝（防大文件耗尽内存）。
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except (ValueError, TypeError) as exc:
        # 非数字 Content-Length（如 "abc"）→ 422，而非 500
        raise ValidationError("Content-Length 头格式非法") from exc
    if content_length > _MAX_CONTENT_LENGTH:
        # 审计：上传被拒绝（超大文件），记录 user/kb/size 便于追溯异常上传行为。
        logger.warning(
            "file_upload_rejected",
            extra={
                "event": "file_upload",
                "outcome": "rejected",
                "reason": "too_large",
                "user_id": str(current_user.id),
                "kb_id": str(kb_id),
                "content_length": content_length,
            },
        )
        raise ValidationError(
            f"文件超 {MAX_DOC_BYTES // 1024 // 1024}MB 上限"
            f"（Content-Length: {content_length} bytes）"
        )
    # security.spec.md§7.2 — content-type 白名单校验。
    if file.content_type not in ALLOWED_MIME_TYPES:
        logger.warning(
            "file_upload_rejected",
            extra={
                "event": "file_upload",
                "outcome": "rejected",
                "reason": "unsupported_mime",
                "user_id": str(current_user.id),
                "kb_id": str(kb_id),
                "mime_type": file.content_type,
            },
        )
        raise ValidationError(
            f"不支持的文件类型 '{file.content_type}'，"
            f"仅允许: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )
    content = (await file.read()).decode("utf-8", errors="replace")
    if not content.strip():
        logger.warning(
            "file_upload_rejected",
            extra={
                "event": "file_upload",
                "outcome": "rejected",
                "reason": "empty_content",
                "user_id": str(current_user.id),
                "kb_id": str(kb_id),
                "title": title,
            },
        )
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
    # 审计：上传成功，记录 user/kb/doc/title/mime/size 便于追溯（不记录原始文件名）。
    logger.info(
        "file_upload_success",
        extra={
            "event": "file_upload",
            "outcome": "success",
            "user_id": str(current_user.id),
            "kb_id": str(kb_id),
            "doc_id": str(doc.id),
            "title": title,
            "mime_type": file.content_type,
            "content_length": content_length,
        },
    )
    return DocumentOut.model_validate(doc)


@router.post("/{kb_id}/search", response_model=list[SearchResult])
async def search_kb(
    kb_id: uuid.UUID,
    payload: SearchQuery,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[SearchResult]:
    return await service.search_kb(session, kb_id, payload)


@router.post("/{kb_id}/rag")
async def rag_query(
    kb_id: uuid.UUID,
    payload: RAGQuery,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    return await service.rag_query(session, kb_id, payload)
