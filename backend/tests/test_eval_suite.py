"""Eval Suite service + judge eval（evals/SPEC.md Success Criteria）。

覆盖 8 项验收：
1. ``create_eval`` 拒绝空 cases（ValidationError）
2. ``run_eval`` 状态流转 pending → running → passed/failed
3. ``run_eval`` 任一 case 抛异常时状态置为 error 并附 finished_at
4. ``score = pass_count / total``，PASSED 当 score ≥ 0.85
5. ``judge_exact`` 归一化空白后精确匹配
6. ``judge_llm`` 输出无法解析 JSON 时返回 passed=False / score=0（不抛错）
7. ``judge_semantic`` 余弦相似度 < 0.75 判定不通过
8. 无 ``predict_fn`` 时用 case 的 actual / expected 自比对

策略：
- SC1/2/3/4/8：经 ``client`` fixture 的 session_factory 调 service.create_eval / run_eval，
  用 predict_fn 控制每条 case 的 actual 输出；SC2 在 predict_fn 中读取 run 状态验证 RUNNING 中间态。
- SC5/6/7：直接测 judge 模块纯函数 / 协程，LLM/semantic 判官用 stub。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.llm_client import LLMResponse
from app.domains.evals import service as eval_service
from app.domains.evals.judge import (
    judge_exact,
    judge_llm,
    judge_semantic,
)
from app.domains.evals.models import (
    EvalCaseInput,
    EvalRun,
    EvalRunCreate,
    EvalStatus,
    JudgeType,
)
from app.main import app

# ===================== 辅助：经 session_factory 执行异步场景 =====================


def _run(
    client: TestClient, scenario: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。"""
    from app.core.database import get_session

    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


# ===================== 1. create_eval 拒绝空 cases =====================


def test_create_eval_rejects_empty_cases(client: TestClient) -> None:
    """create_eval 在 cases 为空时抛 ValidationError（SPEC 1）。"""

    async def _scenario(session: AsyncSession) -> None:
        with pytest.raises(ValidationError):
            await eval_service.create_eval(
                session,
                EvalRunCreate(name="empty", cases=[]),
            )

    _run(client, _scenario)


# ===================== 2. run_eval 状态流转 pending → running → passed/failed =====================


def test_run_eval_status_pending_to_running_to_passed(client: TestClient) -> None:
    """run_eval 状态流转 pending → running → passed（SPEC 2 PASSED 路径）。

    在 predict_fn 中通过 session.get(EvalRun, run.id) 读取当前状态，
    验证执行期为 RUNNING（service 在循环前已 flush RUNNING）。
    """

    async def _scenario(session: AsyncSession) -> None:
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(
                name="pass-flow",
                cases=[
                    EvalCaseInput(input="q1", expected="a1"),
                    EvalCaseInput(input="q2", expected="a2"),
                ],
                judge_type=JudgeType.EXACT,
            ),
        )
        assert run.status == EvalStatus.PENDING.value

        observed: list[str] = []

        async def predict(case: dict[str, Any]) -> str:
            # 执行期读取 run 状态，应见 RUNNING（同 session 内 flush 后可见）
            current = await session.get(EvalRun, run.id)
            observed.append(current.status if current else "?")
            return str(case.get("expected", ""))

        result = await eval_service.run_eval(session, run.id, predict_fn=predict)

        # 执行期观察到的状态应为 RUNNING
        assert observed == [EvalStatus.RUNNING.value, EvalStatus.RUNNING.value]
        # 最终状态 PASSED
        assert result.status == EvalStatus.PASSED.value
        assert result.pass_count == 2
        assert result.score == 1.0
        assert result.started_at is not None
        assert result.finished_at is not None

    _run(client, _scenario)


def test_run_eval_status_pending_to_running_to_failed(client: TestClient) -> None:
    """run_eval 状态流转 pending → running → failed（SPEC 2 FAILED 路径）。"""

    async def _scenario(session: AsyncSession) -> None:
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(
                name="fail-flow",
                cases=[
                    EvalCaseInput(input="q1", expected="right"),
                    EvalCaseInput(input="q2", expected="right"),
                    EvalCaseInput(input="q3", expected="right"),
                ],
                judge_type=JudgeType.EXACT,
            ),
        )
        assert run.status == EvalStatus.PENDING.value

        def predict(case: dict[str, Any]) -> str:
            # 第一条返回错误，其余正确 → 2/3 ≈ 0.667 < 0.85 → FAILED
            return "wrong" if case.get("input") == "q1" else "right"

        result = await eval_service.run_eval(session, run.id, predict_fn=predict)
        assert result.status == EvalStatus.FAILED.value
        assert result.pass_count == 2
        assert result.fail_count == 1
        assert result.score is not None
        assert result.score < 0.85

    _run(client, _scenario)


# ===================== 3. run_eval 异常时状态置 error + finished_at =====================


def test_run_eval_error_status_has_finished_at(client: TestClient) -> None:
    """predict_fn 抛异常时 run 状态置 error 并附 finished_at（SPEC 3）。

    service 通过独立事务持久化 ERROR 状态，session.rollback 后用独立 session 验证。
    """

    async def _scenario(session: AsyncSession) -> None:
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(
                name="error-flow",
                cases=[EvalCaseInput(input="q1", expected="hello")],
                judge_type=JudgeType.EXACT,
            ),
        )

        async def predict(case: dict[str, Any]) -> str:
            raise RuntimeError("predict blew up")

        with pytest.raises(RuntimeError, match="predict blew up"):
            await eval_service.run_eval(session, run.id, predict_fn=predict)

        # 请求 session 在异常时会被 get_session rollback，模拟该行为
        await session.rollback()

        # 用独立 session 读取，验证 ERROR 状态已通过独立事务持久化
        engine = session.bind
        assert engine is not None
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        async with _AsyncSession(bind=engine, expire_on_commit=False) as fresh:
            persisted = await eval_service.get_eval(fresh, run.id)
            assert persisted.status == EvalStatus.ERROR.value
            assert persisted.finished_at is not None

    _run(client, _scenario)


# ===================== 4. score = pass_count / total，PASSED 当 score ≥ 0.85 =====================


def test_run_eval_score_at_threshold_passes(client: TestClient) -> None:
    """score 恰好 ≥ 0.85 时 PASSED（SPEC 4 临界值）。

    7 个 case，6 pass / 1 fail → 6/7 ≈ 0.857 ≥ 0.85 → PASSED。
    """

    async def _scenario(session: AsyncSession) -> None:
        cases = [
            EvalCaseInput(input=f"q{i}", expected="right") for i in range(7)
        ]
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(name="threshold-pass", cases=cases, judge_type=JudgeType.EXACT),
        )

        def predict(case: dict[str, Any]) -> str:
            # q6 返回错误，其余正确
            return "wrong" if case.get("input") == "q6" else "right"

        result = await eval_service.run_eval(session, run.id, predict_fn=predict)
        assert result.pass_count == 6
        assert result.fail_count == 1
        assert result.score is not None
        assert abs(result.score - 6 / 7) < 1e-6
        assert result.score >= 0.85
        assert result.status == EvalStatus.PASSED.value

    _run(client, _scenario)


def test_run_eval_score_below_threshold_fails(client: TestClient) -> None:
    """score 恰好 < 0.85 时 FAILED（SPEC 4 临界值）。

    6 个 case，5 pass / 1 fail → 5/6 ≈ 0.833 < 0.85 → FAILED。
    """

    async def _scenario(session: AsyncSession) -> None:
        cases = [
            EvalCaseInput(input=f"q{i}", expected="right") for i in range(6)
        ]
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(name="threshold-fail", cases=cases, judge_type=JudgeType.EXACT),
        )

        def predict(case: dict[str, Any]) -> str:
            return "wrong" if case.get("input") == "q5" else "right"

        result = await eval_service.run_eval(session, run.id, predict_fn=predict)
        assert result.pass_count == 5
        assert result.fail_count == 1
        assert result.score is not None
        assert abs(result.score - 5 / 6) < 1e-6
        assert result.score < 0.85
        assert result.status == EvalStatus.FAILED.value

    _run(client, _scenario)


# ===================== 5. judge_exact 归一化空白后精确匹配 =====================


def test_judge_exact_normalizes_whitespace() -> None:
    """judge_exact 归一化空白后精确匹配（SPEC 5）。

    _normalize: strip + lower + re.sub(r"\\s+", " ", ...)，故大小写 / 多空白 / 换行
    差异应被消除后视为匹配。
    """
    # 大小写 + 多空白归一化后匹配
    assert judge_exact("Hello   World", "hello world").passed is True
    # 换行 + 制表符归一化后匹配
    assert judge_exact("Hello\n\tWorld", "hello world").passed is True
    # 前后空白归一化后匹配
    assert judge_exact("  hello  ", "hello").passed is True
    # 内容不同则不匹配
    assert judge_exact("hello world", "hello").passed is False
    assert judge_exact("foo", "bar").score == 0.0


# ===== 6. judge_llm 无法解析 JSON 时返回 passed=False / score=0 =====


@pytest.mark.asyncio
async def test_judge_llm_unparseable_json_returns_failed_zero_score() -> None:
    """judge_llm 在 LLM 输出无法解析 JSON 时返回 passed=False / score=0（SPEC 6）。

    不抛错；返回 JudgeResult(passed=False, score=0.0, reason="LLM 输出无法解析")。
    """
    # LLM 返回非 JSON 文本
    stub = MagicMock()
    stub.chat = AsyncMock(return_value=LLMResponse(content="not a json at all"))
    result = await judge_llm("ans", "expected", stub)
    assert result.passed is False
    assert result.score == 0.0
    assert "无法解析" in result.reason


@pytest.mark.asyncio
async def test_judge_llm_missing_score_field_returns_zero() -> None:
    """judge_llm 在 JSON 缺 score 字段时不抛错，回退 score=0（SPEC 6 边界）。"""
    stub = MagicMock()
    stub.chat = AsyncMock(return_value=LLMResponse(content='{"reason": "no score"}'))
    result = await judge_llm("ans", "expected", stub)
    assert result.passed is False  # 0.0 < 0.5
    assert result.score == 0.0


# ===================== 7. judge_semantic 余弦相似度 < 0.75 判定不通过 =====================


@pytest.mark.asyncio
async def test_judge_semantic_below_threshold_fails() -> None:
    """judge_semantic 余弦相似度 < 0.75 判定不通过（SPEC 7）。

    用 stub embedder 构造可控制的向量：
    - 完全相同 → cosine=1.0 ≥ 0.75 → passed=True
    - 正交（0°-90°）→ cosine=0.0 < 0.75 → passed=False
    - 部分重叠（cos=45°，约 0.707）→ < 0.75 → passed=False
    """

    async def _embed_orthogonal(text: str) -> list[float]:
        # actual 文本 → [1, 0]，expected 文本 → [0, 1]，正交 → cosine=0
        return [1.0, 0.0] if "actual" in text else [0.0, 1.0]

    diff = await judge_semantic("actual-x", "expected-y", embedder=_embed_orthogonal)
    assert diff.score == 0.0
    assert diff.passed is False

    async def _embed_same(_: str) -> list[float]:
        return [1.0, 0.0]

    same = await judge_semantic("a", "b", embedder=_embed_same)
    assert same.score == 1.0
    assert same.passed is True

    # cos(45°) ≈ 0.707 < 0.75 阈值
    async def _embed_45(text: str) -> list[float]:
        # actual → [1, 0]，expected → [1, 1]/√2，cosine = 1/√2 ≈ 0.707
        return [1.0, 0.0] if "actual" in text else [1.0, 1.0]

    mid = await judge_semantic("actual-x", "expected-y", embedder=_embed_45)
    assert mid.score is not None
    assert 0.5 < mid.score < 0.75
    assert mid.passed is False


# ===================== 8. 无 predict_fn 时用 case 的 actual / expected 自比对 =====================


def test_run_eval_without_predict_fn_uses_case_actual(client: TestClient) -> None:
    """无 predict_fn 时用 case 的 actual 字段作为预测结果（SPEC 8）。

    service._predict: predict_fn is None → str(case.get("actual", case.get("expected", "")))。
    case 带 actual，actual 与 expected 不同 → FAILED。
    """

    async def _scenario(session: AsyncSession) -> None:
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(
                name="no-predict-actual",
                cases=[
                    EvalCaseInput(
                        input="q1",
                        expected="right",
                        # EvalCaseInput 模型无 actual 字段，通过 metadata 注入
                        metadata={"actual": "wrong"},
                    ),
                ],
                judge_type=JudgeType.EXACT,
            ),
        )

        # run.cases 已 model_dump 为 dict，含 metadata 中的 actual
        # 但 _predict 查找的是 case["actual"] 顶层字段，metadata 中嵌套不生效
        # 需直接修改 run.cases[0] 加入顶层 actual 字段
        run.cases[0]["actual"] = "wrong"
        await session.flush()

        result = await eval_service.run_eval(session, run.id)  # 无 predict_fn
        # actual="wrong" vs expected="right" → 不匹配 → FAILED
        assert result.pass_count == 0
        assert result.fail_count == 1
        assert result.status == EvalStatus.FAILED.value
        assert result.results is not None
        assert result.results[0]["actual"] == "wrong"

    _run(client, _scenario)


def test_run_eval_without_predict_fn_falls_back_to_expected(client: TestClient) -> None:
    """无 predict_fn 且 case 无 actual 时回退用 expected 自比对（SPEC 8 回退路径）。

    service._predict: case.get("actual", case.get("expected", ""))，
    无 actual → 用 expected → expected == expected → 全部通过 → PASSED。
    """

    async def _scenario(session: AsyncSession) -> None:
        run = await eval_service.create_eval(
            session,
            EvalRunCreate(
                name="no-predict-expected",
                cases=[
                    EvalCaseInput(input="q1", expected="right"),
                    EvalCaseInput(input="q2", expected="right"),
                ],
                judge_type=JudgeType.EXACT,
            ),
        )

        result = await eval_service.run_eval(session, run.id)  # 无 predict_fn
        # 无 actual → 回退 expected → expected == expected → PASSED
        assert result.pass_count == 2
        assert result.fail_count == 0
        assert result.score == 1.0
        assert result.status == EvalStatus.PASSED.value
        assert result.results is not None
        assert result.results[0]["actual"] == "right"

    _run(client, _scenario)
