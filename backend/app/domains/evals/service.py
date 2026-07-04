"""Eval Suite — 业务逻辑纯函数。"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
)

from app.core.config import settings
from app.core.exceptions import LLMError, NotFoundError, ValidationError
from app.core.llm_client import LLMClient, LLMConfig, get_llm_client
from app.domains.evals.judge import (
    JudgeResult,
    judge_contains,
    judge_exact,
    judge_llm_with_sampling,
    judge_semantic,
)
from app.domains.evals.models import (
    CaseResult,
    EvalRun,
    EvalRunCreate,
    EvalStatus,
    JudgeType,
)

logger = logging.getLogger("app.evals.service")

# P1-6：regression 阈值。当前 score 低于 baseline 超过此值则标回归。
_REGRESSION_THRESHOLD = 0.05
# P1-6：LLM judge 多采样次数（抑制 ±0.1 噪声）。
_LLM_JUDGE_SAMPLES = 3


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
    """执行 eval。predict_fn(case) -> actual；默认用 expected 直接比对。

    失败状态独立事务：执行期抛错时，ERROR 状态通过独立 session 落库，
    避免被请求级 ``get_session`` 的 rollback 吞掉（之前 flush 后 raise 导致
    状态丢失）。LLM 判官客户端走应用级单例（P1-5 ``get_llm_client``），
    整个 run_eval 生命周期复用，关闭由 app shutdown 的 ``close_all_clients``
    统一负责，run_eval 不再 close。

    注：evals 为批处理任务，LLM 调用期间持有 DB 连接的影响小于 agents 的
    请求路径 ReAct 循环（agents 已在 ``execute_agent`` 中 commit 释放连接）。
    """
    run = await get_eval(session, eval_id)
    run.status = EvalStatus.RUNNING.value
    run.started_at = datetime.now(UTC)
    await session.flush()

    # P1-6：查同 name 上次成功 run 的 score 作为基线（golden dataset 回归对比）
    baseline = await _fetch_baseline_score(session, run.name, eval_id)
    run.baseline_score = baseline

    # LLM 判官客户端仅在需要时获取单例（P1-5），整个 run_eval 生命周期复用。
    # 关闭由 app lifespan shutdown 的 close_all_clients 负责，此处不 close。
    client = _build_llm_client_if_needed(run.judge_type)
    results: list[CaseResult] = []
    pass_count = 0
    try:
        for case in run.cases:
            actual = await _predict(predict_fn, case)
            result = await _judge_case(run.judge_type, case, actual, client)
            results.append(result)
            if result.passed:
                pass_count += 1
    except Exception:  # noqa: BLE001
        # predict_fn 为调用方注入的任意可调用对象，可能抛任意异常；
        # 任何异常都需把 eval 标记为 ERROR 状态后重抛，由调用方决定如何处理。
        # 先取 bind（可能为 None），再持久化；访问 session.bind 本身不在 try 内
        # 可避免二次异常掩盖原始错误。
        bind = session.bind
        await _persist_error_status(bind, eval_id)
        raise

    run.results = [r.model_dump(mode="json") for r in results]
    run.pass_count = pass_count
    run.fail_count = len(results) - pass_count
    run.score = pass_count / len(results) if results else 0.0
    run.status = EvalStatus.PASSED.value if run.score >= 0.85 else EvalStatus.FAILED.value
    # P1-6：regression 检测。score 低于 baseline 超阈值则标回归。
    run.is_regression = _detect_regression(run.score, baseline)
    run.finished_at = datetime.now(UTC)
    await session.flush()
    return run


async def _fetch_baseline_score(
    session: AsyncSession, name: str, current_id: uuid.UUID
) -> float | None:
    """P1-6：查询同 name 的上次成功 run 的 score 作为基线。

    基线 = 同 name 中 created_at 早于当前 run、status ∈ {PASSED, FAILED}
    的最近一条 run 的 score。用于回归检测（当前 score vs 基线）。
    """
    stmt = (
        select(EvalRun.score)
        .where(
            EvalRun.name == name,
            EvalRun.id != current_id,
            EvalRun.status.in_([EvalStatus.PASSED.value, EvalStatus.FAILED.value]),
            EvalRun.score.is_not(None),
        )
        .order_by(EvalRun.created_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return float(row) if row is not None else None


def _detect_regression(score: float | None, baseline: float | None) -> bool:
    """P1-6：判断是否回归。score 低于 baseline 超阈值则回归。

    无 baseline（首次 run）不算回归。score 或 baseline 为 None 不算。
    """
    if score is None or baseline is None:
        return False
    return (baseline - score) > _REGRESSION_THRESHOLD


async def _persist_error_status(
    bind: AsyncEngine | AsyncConnection | None, eval_id: uuid.UUID
) -> None:
    """用独立 session 落库 ERROR 状态。

    请求级 ``get_session`` 在异常时会 rollback，导致在请求 session 上 flush 的
    ERROR 状态被丢弃。此处用独立 session + 独立事务，保证 ERROR 状态持久化，
    即便请求 session 回滚也不受影响。

    接收 bind（``AsyncEngine | AsyncConnection | None``）而非 session，避免调用方
    commit 后 session 内部状态变化导致的 ``MissingGreenlet`` 问题。
    """
    if bind is None:
        # 无可用 engine 时退化为不落库（仅日志），不阻塞异常传播。
        logger.warning("no engine bound to session; skip persisting ERROR status")
        return
    try:
        async with AsyncSession(bind=bind, expire_on_commit=False) as err_session:
            await err_session.execute(
                update(EvalRun)
                .where(EvalRun.id == eval_id)
                .values(
                    status=EvalStatus.ERROR.value,
                    finished_at=datetime.now(UTC),
                )
            )
            await err_session.commit()
    except Exception:  # noqa: BLE001
        # 落库失败不应掩盖原始异常，仅记录日志。
        logger.exception("failed to persist ERROR status for eval %s", eval_id)


async def _predict(predict_fn: Any | None, case: dict[str, Any]) -> str:
    """获取预测结果。无 predict_fn 时回退用 case 的 actual 字段。"""
    if predict_fn is None:
        return str(case.get("actual", case.get("expected", "")))
    result = predict_fn(case)
    if hasattr(result, "__await__"):
        result = await result
    return str(result)


async def _judge_case(
    judge_type: str,
    case: dict[str, Any],
    actual: str,
    llm_client: LLMClient | None,
) -> CaseResult:
    """根据判官类型评估单条 case。LLM 判官复用传入的 client。"""
    expected = str(case.get("expected", ""))

    def _exact() -> JudgeResult:
        return judge_exact(actual, expected)

    def _contains() -> JudgeResult:
        return judge_contains(actual, expected)

    async def _semantic() -> JudgeResult:
        return await judge_semantic(actual, expected)

    async def _llm() -> JudgeResult:
        if llm_client is None:
            raise LLMError("LLM 判官需要客户端，但未提供")
        # P1-6：多采样取均值，抑制 LLM judge ±0.1 噪声
        return await judge_llm_with_sampling(
            actual, expected, llm_client, samples=_LLM_JUDGE_SAMPLES
        )

    judge_map: dict[str, Any] = {
        JudgeType.EXACT.value: _exact,
        JudgeType.CONTAINS.value: _contains,
        JudgeType.SEMANTIC.value: _semantic,
        JudgeType.LLM.value: _llm,
    }
    handler = judge_map.get(judge_type)
    if handler is None:
        raise LLMError(f"未知判官类型: {judge_type}")
    result_or_coro = handler()
    if hasattr(result_or_coro, "__await__"):
        judge_result: JudgeResult = await result_or_coro
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


def _build_llm_client_if_needed(judge_type: str) -> LLMClient | None:
    """仅当判官类型为 LLM 时获取单例客户端（P1-5），避免无谓的 httpx 连接。

    复用 ``get_llm_client`` 缓存：相同 provider+base_url+api_key+model 的配置
    在应用生命周期内共享同一个 httpx 连接池。关闭由 app shutdown 统一负责，
    调用方不应自行 close。
    """
    if judge_type != JudgeType.LLM.value:
        return None
    return get_llm_client(
        LLMConfig(
            provider="openai",
            model=settings.default_llm_model,
            api_key=settings.openai_api_key,
        )
    )


__all__ = ["create_eval", "get_eval", "list_evals", "run_eval"]
