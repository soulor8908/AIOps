"""文档分块器 — 固定长度分块（约 50 行）。

支持 chunk_size + overlap。按字符切分（中文友好，不按单词）。
可在句末边界对齐，避免切断句子。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ChunkResult:
    """分块结果。"""

    index: int
    content: str
    token_count: int


def chunk_text(
    text: str,
    chunk_size: int = 800,
    overlap: int = 100,
) -> list[ChunkResult]:
    """固定长度分块。overlap 必须小于 chunk_size。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必须满足 0 <= overlap < chunk_size")
    text = text.strip()
    if not text:
        return []
    chunks: list[ChunkResult] = []
    start = 0
    step = chunk_size - overlap
    idx = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        piece = text[start:end].strip()
        if piece:
            chunks.append(
                ChunkResult(index=idx, content=piece, token_count=_estimate_tokens(piece))
            )
            idx += 1
        if end >= len(text):
            break
        start += step
    return chunks


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：英文按词，中文按字。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_words = len(text.split())
    return cjk + max(ascii_words - cjk, 0)
