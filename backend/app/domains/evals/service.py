"""Eval Suite — 业务逻辑纯函数。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import LLMError, NotFoundError, ValidationError
from app.core.llm_client import LLMClient, LLMConfig
from app.core.config import settings
from app.domains.evals.judge import (
    JudgeResult,
    judge_contains,
    judge_exact,
    judge_llm,
    judge_semantic,
)
from app.domains.evals.models import (
    CaseResult,
    EvalRun,
    EvalRunCreate,
    EvalStatus,
    JudgeType,
)


async def create_eval(session: AsyncSession, payload: EvalRunCreate) -> EvalRun:
    """创建 eval。cases 必须非空。"""
    if not payload.cases:
        raise ValidationError("eval 至少需要一个 case")
    run = EvalRun(
        name=payload.name,
        description=payload.description,
        rules=[r.model_dump(mode="json") for r in payload.rules],
        cases=[c.model_dump(mode="json") for c in payload.cases],
        judge_type=payload.judge_type.value,
        status=EvalStatus.PENDING.value,
    )
    session.add(run)
    await session.flush()
    return run


async def get_eval(session: AsyncSession, eval_id: uuid.UUID) -> EvalRun:
    """获取 eval。"""
    run = await session.get(EvalRun, eval_id)
    if run is None:
        raise NotFoundError(f"Eval {eval_id} 不存在")
    return run


async def list_evals(
    session: AsyncSession, limit: int = 50, offset: int = 0
) -> list[EvalRun]:
    """列出 eval。"""
    stmt = select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def run_eval(
    session: AsyncSession,
    eval_id: uuid.UUID,
    predict_fn: Any | None = None,
) -> EvalRun:
    """执行 eval。predict_fn(case) -> actual；默认用 expected 直接比对。"""
    run = await get_eval(session, eval_id)
    run.status = EvalStatus.RUNNING.value
    run.started_at = datetime.now(timezone.utc)
    await session.flush()

    results: list[CaseResult] = []
    pass_count = 0
    try:
        for case in run.cases:
            actual = await _predict(predict_fn, case)
            result = await _judge_case(run.judge_type, case, actual)
            results.append(result)
            if result.passed:
                pass_count += 1
    except Exception:
        run.status = EvalStatus.ERROR.value
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
        raise

    run.results = [r.model_dump(mode="json") for r in results]
    run.pass_count = pass_count
    run.fail_count = len(results) - pass_count
    run.score = pass_count / len(results) if results else 0.0
    run.status = EvalStatus.PASSED.value if run.score >= 0.85 else EvalStatus.FAILED.value
    run.finished_at = datetime.now(timezone.utc)
    await session.flush()
    return run


async def _predict(predict_fn: Any | None, case: dict[str, Any]) -> str:
    """获取预测结果。无 predict_fn 时回退用 case 的 actual 字段。"""
    if predict_fn is None:
        return str(case.get("actual", case.get("expected", "")))
    result = predict_fn(case)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return str(result)


async def _judge_case(
    judge_type: str, case: dict[str, Any], actual: str
) -> CaseResult:
    """根据判官类型评估单条 case。"""
    expected = str(case.get("expected", ""))
    judge_map: dict[str, Any] = {
        JudgeType.EXACT.value: lambda: judge_exact(actual, expected),
        JudgeType.CONTAINS.value: lambda: judge_contains(actual, expected),
        JudgeType.SEMANTIC.value: lambda: judge_semantic(actual, expected),
        JudgeType.LLM.value: lambda: judge_llm(actual, expected, _default_llm_client()),
    }
    handler = judge_map.get(judge_type)
    if handler is None:
        raise LLMError(f"未知判官类型: {judge_type}")
    result_or_coro = handler()
    if hasattr(result_or_coro, "__await__"):
        judge_result: JudgeResult = await result_or_coro  # type: ignore[assignment]
    else:
        judge_result = result_or_coro
    return CaseResult(
        case_name=case.get("name"),
        input=str(case.get("input", "")),
        expected=expected or None,
        actual=actual,
        passed=judge_result.passed,
        score=judge_result.score,
        reason=judge_result.reason,
    )


def _default_llm_client() -> LLMClient:
    """构造默认 LLM 客户端供 LLM 判官使用。"""
    return LLMClient(
        LLMConfig(
            provider="openai",
            model=settings.default_llm_model,
            api_key=settings.openai_api_key,
        )
    )


__all__ = ["create_eval", "get_eval", "list_evals", "run_eval"]
