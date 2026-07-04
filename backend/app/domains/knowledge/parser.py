"""文档文本提取 — 按 content-type 分发到对应解析器。

支持 text/plain、text/markdown（UTF-8 decode）、application/pdf（pypdf）、
DOCX（python-docx）。提取失败一律抛 ``ValidationError``，避免二进制被静默
decode 成 U+FFFD 垃圾 embedding（security.spec.md§7.2）。

设计要点
--------
- 文本类走 UTF-8 **严格** decode：非法字节直接报错而非替换为 U+FFFD。
- PDF/DOCX 在提取前先校验原始字节大小（沿用 ``MAX_DOC_BYTES``），防止
  超大文件在内存中展开耗尽资源。
- 解析库的各类异常统一转 ``ValidationError``，对调用方暴露单一错误类型。
"""

from __future__ import annotations

import io

from docx import Document
from pypdf import PdfReader

from app.core.exceptions import ValidationError
from app.domains.knowledge.service import MAX_DOC_BYTES

# docx 的 MIME 类型（application/vnd.openxmlformats-officedocument.wordprocessingml.document）
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"

_TEXT_MIMES: frozenset[str] = frozenset({"text/plain", "text/markdown"})


def extract_text(content: bytes, content_type: str) -> str:
    """从上传内容提取纯文本。

    - text/plain, text/markdown: UTF-8 decode
    - application/pdf: pypdf 提取
    - docx: python-docx 提取
    - 提取失败抛 ValidationError（不要静默损坏成 U+FFFD）
    """
    if content_type in _TEXT_MIMES:
        return _extract_text_plain(content)
    if content_type == PDF_MIME:
        return _extract_pdf(content)
    if content_type == DOCX_MIME:
        return _extract_docx(content)
    raise ValidationError(f"不支持的文件类型 '{content_type}'")


def _check_size(content: bytes) -> None:
    """大文件防护：提取前校验原始字节大小（沿用 ``MAX_DOC_BYTES`` 上限）。"""
    if len(content) > MAX_DOC_BYTES:
        raise ValidationError(
            f"文件超 {MAX_DOC_BYTES // 1024 // 1024}MB 上限"
            f"（实际 {len(content)} bytes, 上限 {MAX_DOC_BYTES} bytes）"
        )


def _extract_text_plain(content: bytes) -> str:
    """UTF-8 严格 decode：非法字节抛 ValidationError，不静默替换为 U+FFFD。"""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"UTF-8 解码失败：{exc}") from exc


def _extract_pdf(content: bytes) -> str:
    """pypdf 提取 PDF 文本。解析失败或无文本抛 ValidationError。"""
    _check_size(content)
    try:
        reader = PdfReader(io.BytesIO(content))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        extracted = "\n".join(parts)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"PDF 文本提取失败：{exc}") from exc
    if not extracted.strip():
        # 扫描件 / 纯图片 PDF 提取不到文本，拒绝以避免产出空 embedding
        raise ValidationError("PDF 未提取到任何文本（可能为扫描件或空文件）")
    return extracted


def _extract_docx(content: bytes) -> str:
    """python-docx 提取 DOCX 段落文本。解析失败或无文本抛 ValidationError。"""
    _check_size(content)
    try:
        document = Document(io.BytesIO(content))
        extracted = "\n".join(p.text for p in document.paragraphs)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"DOCX 文本提取失败：{exc}") from exc
    if not extracted.strip():
        raise ValidationError("DOCX 未提取到任何文本（可能为空文档）")
    return extracted


__all__ = ["DOCX_MIME", "PDF_MIME", "extract_text"]
