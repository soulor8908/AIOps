"""Knowledge Base — FastAPI 路由。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_admin, get_current_user
from app.core.exceptions import GatewayTimeoutError, ValidationError
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
from app.domains.knowledge.parser import DOCX_MIME, extract_text
from app.domains.knowledge.service import MAX_DOC_BYTES

logger = logging.getLogger("app.audit.knowledge")

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


async def _with_request_timeout(coro: Any) -> Any:
    """P0-20：请求级超时包裹（rag_query 用）。超时抛 504 gateway_timeout。"""
    try:
        return await asyncio.wait_for(
            coro, timeout=settings.agent_execute_timeout_seconds
        )
    except TimeoutError as exc:
        raise GatewayTimeoutError(
            f"请求超 {settings.agent_execute_timeout_seconds}s 超时"
        ) from exc

# security.spec.md§7.2 — 文件上传 content-type 白名单。
# 文本类直接 UTF-8 decode；PDF/DOCX 由 parser 模块走专用提取器
# （pypdf / python-docx），避免二进制当 UTF-8 decode 静默损坏成 U+FFFD。
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"text/plain", "text/markdown", "application/pdf", DOCX_MIME}
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
    # P4-1：非 admin 仅能查看自己的 KB
    owner_id = None if current_user.is_admin else current_user.id
    kbs = await service.list_kbs(session, limit=limit, offset=offset, owner_id=owner_id)
    return [KnowledgeBaseOut.model_validate(k) for k in kbs]


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_kb(
    payload: KnowledgeBaseCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_admin),
) -> KnowledgeBaseOut:
    # P4-1：绑定当前 admin 为 owner
    kb = await service.create_kb(session, payload, owner_id=current_user.id)
    return KnowledgeBaseOut.model_validate(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_kb(
    kb_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBaseOut:
    # P4-1：非 admin 校验所有权
    owner_id = None if current_user.is_admin else current_user.id
    kb = await service.get_kb(session, kb_id, owner_id=owner_id)
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
    # parser 按 content-type 分发：文本走 UTF-8 decode，PDF/DOCX 走专用提取器。
    # file.content_type 经白名单校验后必为 ALLOWED_MIME_TYPES 中的非空字符串，
    # ``or ""`` 仅用于将 ``str | None`` 收敛为 ``str`` 以满足类型签名。
    raw = await file.read()
    content = extract_text(raw, file.content_type or "")
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
    # P4-1：非 admin 校验 KB 写权限(上传文档需拥有该 KB)
    owner_id = None if current_user.is_admin else current_user.id
    doc = await service.upload_document(
        session,
        kb_id,
        title=title,
        content=content,
        mime_type=file.content_type,
        source_uri=source_uri,
        owner_id=owner_id,
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
    # P4-1：非 admin 校验所有权
    owner_id = None if current_user.is_admin else current_user.id
    return await service.search_kb(session, kb_id, payload, owner_id=owner_id)


@router.post("/{kb_id}/rag")
async def rag_query(
    kb_id: uuid.UUID,
    payload: RAGQuery,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    # P4-1：非 admin 校验所有权
    owner_id = None if current_user.is_admin else current_user.id
    # P0-20：请求级超时，超时抛 504 gateway_timeout
    return await _with_request_timeout(
        service.rag_query(session, kb_id, payload, owner_id=owner_id)
    )
