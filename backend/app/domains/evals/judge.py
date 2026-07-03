"""LLM-as-judge 实现（约 60 行）。

四种判官：
- judge_exact: 精确匹配（归一化空白后）
- judge_contains: 子串包含
- judge_llm: 调用 LLM 打分（0-1）
- judge_semantic: 余弦相似度（embed 后比较）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import LLMError
from app.core.llm_client import LLMClient, Message


@dataclass(slots=True)
class JudgeResult:
    """判官结果。"""

    passed: bool
    score: float
    reason: str = ""


def _normalize(text: str) -> str:
    """归一化：去多余空白与标点差异。"""
    return re.sub(r"\s+", " ", text.strip().lower())


def judge_exact(actual: str, expected: str) -> JudgeResult:
    """精确匹配。"""
    ok = _normalize(actual) == _normalize(expected)
    return JudgeResult(passed=ok, score=1.0 if ok else 0.0, reason="exact match")


def judge_contains(actual: str, expected: str) -> JudgeResult:
    """子串包含。"""
    ok = _normalize(expected) in _normalize(actual)
    return JudgeResult(passed=ok, score=1.0 if ok else 0.0, reason="substring match")


async def judge_llm(
    actual: str,
    expected: str,
    client: LLMClient,
    criteria: str = "回答是否准确且相关",
) -> JudgeResult:
    """LLM 打分。返回 0-1 浮点。"""
    prompt = (
        f"你是严格判官。根据准则判断回答质量。\n"
        f"准则：{criteria}\n"
        f"期望：{expected}\n"
        f"实际：{actual}\n"
        f"只输出 JSON: {{\"score\": 0.0-1.0, \"reason\": \"...\"}}"
    )
    try:
        resp = await client.chat([Message(role="user", content=prompt)])
    except LLMError as exc:
        # LLMClient.chat 已将所有 HTTP/JSON/结构异常统一包装为 LLMError，
        # 此处仅重抛并附加上下文，无需捕获更宽异常。
        raise LLMError(f"LLM 判官调用失败: {exc}") from exc
    import json

    try:
        data = json.loads(resp.content.strip().strip("`").strip())
        score = float(data.get("score", 0.0))
        reason = str(data.get("reason", ""))
    except (json.JSONDecodeError, ValueError, TypeError):
        return JudgeResult(passed=False, score=0.0, reason="LLM 输出无法解析")
    return JudgeResult(passed=score >= 0.5, score=score, reason=reason)


async def judge_semantic(
    actual: str, expected: str, embedder: Any = None
) -> JudgeResult:
    """语义相似度。embed 后余弦相似度。"""
    from app.domains.knowledge.embedder import embed_text

    get_embed = embedder or embed_text
    actual_vec = await get_embed(actual)
    expected_vec = await get_embed(expected)
    score = _cosine(actual_vec, expected_vec)
    return JudgeResult(
        passed=score >= 0.75, score=score, reason=f"cosine={score:.3f}"
    )


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


__all__ = ["JudgeResult", "judge_contains", "judge_exact", "judge_llm", "judge_semantic"]
