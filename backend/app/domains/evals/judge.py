"""LLM-as-judge 实现。

四种判官：
- judge_exact: 精确匹配（归一化空白后）
- judge_contains: 子串包含
- judge_llm: 调用 LLM 打分（0-1），P0-3 起用 structured output 强约束 JSON
- judge_semantic: 余弦相似度（embed 后比较）

P0-3 修复：
- 旧实现用 ``strip().strip("`").strip()`` 解析 LLM 输出的 JSON，markdown 包裹
  时必崩。改用 ``response_format`` json_schema 强约束（OpenAI）或重试解析（Anthropic
  不支持 response_format，用 json.loads + 容错）。
- ``judge_llm_with_sampling`` 多次采样取均值，抑制 LLM judge ±0.1 噪声，
  使 0.85 阈值有统计意义（P1-6）。
"""

from __future__ import annotations

import json
import re
import statistics
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


# P0-3：structured output 的 JSON schema，强约束 LLM 输出 {score, reason}
_JUDGE_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_result",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "score": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["score", "reason"],
            "additionalProperties": False,
        },
    },
}


async def judge_llm(
    actual: str,
    expected: str,
    client: LLMClient,
    criteria: str = "回答是否准确且相关",
) -> JudgeResult:
    """LLM 打分。返回 0-1 浮点。

    P0-3：用 ``response_format`` json_schema 强约束输出结构（OpenAI），
    替代 ``strip("`")`` 字符串 hack。Anthropic 不支持 response_format，
    走 json.loads + 容错解析。
    """
    prompt = (
        f"你是严格判官。根据准则判断回答质量。\n"
        f"准则：{criteria}\n"
        f"期望：{expected}\n"
        f"实际：{actual}\n"
        f"只输出 JSON: {{\"score\": 0.0-1.0, \"reason\": \"...\"}}"
    )
    try:
        resp = await client.chat(
            [Message(role="user", content=prompt)],
            response_format=_JUDGE_RESPONSE_FORMAT,
        )
    except LLMError as exc:
        # LLMClient.chat 已将所有 HTTP/JSON/结构异常统一包装为 LLMError，
        # 此处仅重抛并附加上下文，无需捕获更宽异常。
        raise LLMError(f"LLM 判官调用失败: {exc}") from exc
    return _parse_judge_json(resp.content)


async def judge_llm_with_sampling(
    actual: str,
    expected: str,
    client: LLMClient,
    criteria: str = "回答是否准确且相关",
    samples: int = 3,
) -> JudgeResult:
    """多次采样取均值（P1-6）。

    LLM judge 有 ±0.1 噪声，单次采样 0.85 阈值无统计意义。
    多次采样取均值 + 计算标准差，使阈值判定可靠。

    ``samples`` 默认 3：成本与稳定性的平衡点。1 = 退化为单次（向后兼容）。
    """
    if samples <= 1:
        return await judge_llm(actual, expected, client, criteria)
    scores: list[float] = []
    last_reason = ""
    for _ in range(samples):
        result = await judge_llm(actual, expected, client, criteria)
        scores.append(result.score)
        last_reason = result.reason
    mean_score = statistics.mean(scores)
    stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0
    reason = f"mean={mean_score:.3f} stdev={stdev:.3f} n={samples}; {last_reason}"
    return JudgeResult(passed=mean_score >= 0.5, score=mean_score, reason=reason)


def _parse_judge_json(content: str) -> JudgeResult:
    """解析 LLM 判官 JSON 输出。

    P0-3：response_format 强约束下 OpenAI 直接返回 JSON 字符串。
    兜底处理 markdown 包裹（Anthropic / 非 strict 模型），用正则提取首个
    JSON 对象，替代 ``strip("`")`` 字符串 hack。
    """
    text = content.strip()
    # 直接解析
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：提取 markdown 代码块或首个 JSON 对象
        data = _extract_json(text)
    if data is None:
        return JudgeResult(passed=False, score=0.0, reason="LLM 输出无法解析")
    try:
        score = float(data.get("score", 0.0))
        reason = str(data.get("reason", ""))
    except (ValueError, TypeError):
        return JudgeResult(passed=False, score=0.0, reason="LLM 输出 score 字段无效")
    return JudgeResult(passed=score >= 0.5, score=score, reason=reason)


def _extract_json(text: str) -> dict[str, object] | None:
    """从 markdown 代码块或混排文本中提取首个 JSON 对象。

    P0-3 替代 ``strip("`").strip()`` hack：覆盖 `````json ... ``` ```、
    裸 ``{...}``、前后有说明文字等情况。
    """
    # 优先匹配 ```json ... ``` 或 ``` ... ```
    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block:
        try:
            parsed = json.loads(code_block.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    # 兜底：首个 {...} 块
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    return None


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


__all__ = [
    "JudgeResult",
    "judge_contains",
    "judge_exact",
    "judge_llm",
    "judge_llm_with_sampling",
    "judge_semantic",
]
