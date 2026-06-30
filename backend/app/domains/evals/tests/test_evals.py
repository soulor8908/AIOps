"""Eval Suite — 单元测试。

覆盖 service + judge 纯函数。LLM/semantic 判官用 stub。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.exceptions import NotFoundError, ValidationError
from app.core.llm_client import LLMResponse
from app.domains.evals.judge import (
    judge_contains,
    judge_exact,
    judge_semantic,
    judge_llm,
)
from app.domains.evals.models import (
    EvalCaseInput,
    EvalRunCreate,
    EvalRuleInput,
    EvalStatus,
    JudgeType,
)
from app.domains.evals import service


@pytest_asyncio.fixture
async def session() -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ===== judge 单元测试 =====

def test_judge_exact_match() -> None:
    result = judge_exact("Hello World", "hello world")
    assert result.passed is True
    assert result.score == 1.0


def test_judge_exact_mismatch() -> None:
    result = judge_exact("foo", "bar")
    assert result.passed is False
    assert result.score == 0.0


def test_judge_contains() -> None:
    assert judge_contains("the quick brown fox", "brown").passed is True
    assert judge_contains("foo", "bar").passed is False


@pytest.mark.asyncio
async def test_judge_llm_parses_score() -> None:
    stub = MagicMock()
    stub.chat = AsyncMock(
        return_value=LLMResponse(content='{"score": 0.9, "reason": "good"}')
    )
    result = await judge_llm("ans", "expected", stub)
    assert result.passed is True
    assert result.score == 0.9


@pytest.mark.asyncio
async def test_judge_semantic_cosine() -> None:
    async def fake_embed(text: str) -> list[float]:
        return [1.0, 0.0] if "a" in text else [0.0, 1.0]

    same = await judge_semantic("aaa", "aaa", embedder=fake_embed)
    assert same.score == 1.0
    diff = await judge_semantic("aaa", "bbb", embedder=fake_embed)
    assert diff.score == 0.0
    assert diff.passed is False


# ===== service 测试 =====

@pytest.mark.asyncio
async def test_create_eval_requires_cases(session: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await service.create_eval(
            session,
            EvalRunCreate(name="e", cases=[]),
        )


@pytest.mark.asyncio
async def test_create_and_get_eval(session: AsyncSession) -> None:
    run = await service.create_eval(
        session,
        EvalRunCreate(
            name="basic",
            cases=[EvalCaseInput(input="hi", expected="hello")],
            judge_type=JudgeType.EXACT,
        ),
    )
    assert run.status == EvalStatus.PENDING.value
    fetched = await service.get_eval(session, run.id)
    assert fetched.id == run.id


@pytest.mark.asyncio
async def test_get_eval_not_found(session: AsyncSession) -> None:
    import uuid
    with pytest.raises(NotFoundError):
        await service.get_eval(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_run_eval_exact_judge(session: AsyncSession) -> None:
    """predict_fn 返回正确值，应全部通过。"""
    run = await service.create_eval(
        session,
        EvalRunCreate(
            name="pass-all",
            cases=[
                EvalCaseInput(input="q1", expected="answer1"),
                EvalCaseInput(input="q2", expected="answer2"),
            ],
            judge_type=JudgeType.EXACT,
        ),
    )

    async def predict(case: dict[str, Any]) -> str:
        return str(case.get("expected", ""))

    result = await service.run_eval(session, run.id, predict_fn=predict)
    assert result.status == EvalStatus.PASSED.value
    assert result.pass_count == 2
    assert result.fail_count == 0
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_run_eval_failed_below_threshold(session: AsyncSession) -> None:
    run = await service.create_eval(
        session,
        EvalRunCreate(
            name="fail-some",
            cases=[
                EvalCaseInput(input="q1", expected="right"),
                EvalCaseInput(input="q2", expected="right"),
                EvalCaseInput(input="q3", expected="right"),
            ],
            judge_type=JudgeType.EXACT,
        ),
    )

    def predict(case: dict[str, Any]) -> str:
        # 第一个返回错误，其余正确
        idx = case.get("input")
        return "wrong" if idx == "q1" else "right"

    result = await service.run_eval(session, run.id, predict_fn=predict)
    assert result.pass_count == 2
    assert result.fail_count == 1
    assert result.score < 0.85
    assert result.status == EvalStatus.FAILED.value


@pytest.mark.asyncio
async def test_run_eval_contains_judge(session: AsyncSession) -> None:
    run = await service.create_eval(
        session,
        EvalRunCreate(
            name="contains-test",
            cases=[EvalCaseInput(input="q", expected="hello")],
            judge_type=JudgeType.CONTAINS,
        ),
    )

    async def predict(case: dict[str, Any]) -> str:
        return "well hello there"

    result = await service.run_eval(session, run.id, predict_fn=predict)
    assert result.pass_count == 1
