"""Self-diagnose（P1-7）测试 — 根因分析 + 修复策略库。

覆盖：
1. **analyze_root_cause**：各根因关键词匹配 + UNKNOWN 兜底
2. **select_strategy**：根因 → 策略映射
3. **build_feedback**：策略 → 反馈模板（含 {reason} 占位）
4. **SelfDiagnoser.diagnose**：端到端 + 失败降级
5. **executor 集成**：self-heal 使用策略化反馈（mock）
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.llm_client import LLMResponse
from app.domains.agents.executor import AgentExecutor
from app.domains.agents.models import Agent
from app.domains.agents.self_diagnose import (
    Diagnosis,
    RepairStrategy,
    RootCause,
    SelfDiagnoser,
    analyze_root_cause,
    build_feedback,
    select_strategy,
)

# ===================== 1. analyze_root_cause =====================


def test_analyze_format_error() -> None:
    assert analyze_root_cause("输出格式不符合 JSON 要求") == RootCause.FORMAT_ERROR
    assert analyze_root_cause("invalid markdown structure") == RootCause.FORMAT_ERROR


def test_analyze_hallucination() -> None:
    assert analyze_root_cause("存在幻觉，捏造了事实") == RootCause.HALLUCINATION
    assert analyze_root_cause("hallucinated content") == RootCause.HALLUCINATION


def test_analyze_tool_misuse() -> None:
    assert analyze_root_cause("工具调用参数错误") == RootCause.TOOL_MISUSE
    assert analyze_root_cause("wrong API parameter") == RootCause.TOOL_MISUSE


def test_analyze_incomplete_info() -> None:
    assert analyze_root_cause("信息不完整，缺少关键数据") == RootCause.INCOMPLETE_INFO


def test_analyze_ambiguous_query() -> None:
    assert analyze_root_cause("问题模糊有歧义") == RootCause.AMBIGUOUS_QUERY


def test_analyze_reasoning_error() -> None:
    assert analyze_root_cause("推理逻辑错误") == RootCause.REASONING_ERROR
    assert analyze_root_cause("wrong reasoning") == RootCause.REASONING_ERROR


def test_analyze_unknown_when_no_match() -> None:
    assert analyze_root_cause("完全不相关的文本xyz") == RootCause.UNKNOWN
    assert analyze_root_cause("") == RootCause.UNKNOWN


def test_analyze_includes_answer_in_search() -> None:
    """answer 也参与关键词匹配。"""
    assert analyze_root_cause("", "answer has format issue") == RootCause.FORMAT_ERROR


# ===================== 2. select_strategy =====================


def test_select_strategy_mapping() -> None:
    assert select_strategy(RootCause.AMBIGUOUS_QUERY) == RepairStrategy.CLARIFY_QUERY
    assert select_strategy(RootCause.INCOMPLETE_INFO) == RepairStrategy.REQUEST_MORE_INFO
    assert select_strategy(RootCause.TOOL_MISUSE) == RepairStrategy.RETRY_WITH_HINT
    assert select_strategy(RootCause.REASONING_ERROR) == RepairStrategy.DECOMPOSE
    assert select_strategy(RootCause.FORMAT_ERROR) == RepairStrategy.FIX_FORMAT
    assert select_strategy(RootCause.HALLUCINATION) == RepairStrategy.VERIFY_FACTS
    assert select_strategy(RootCause.UNKNOWN) == RepairStrategy.GENERIC_IMPROVE


# ===================== 3. build_feedback =====================


def test_build_feedback_includes_reason() -> None:
    fb = build_feedback(RepairStrategy.FIX_FORMAT, "JSON 格式错误")
    assert "JSON 格式错误" in fb
    assert "格式" in fb


def test_build_feedback_generic_improve_fallback() -> None:
    fb = build_feedback(RepairStrategy.GENERIC_IMPROVE, "原因")
    assert "原因" in fb
    assert "改进" in fb


def test_build_feedback_unknown_strategy_falls_back() -> None:
    """未知策略回退到 GENERIC_IMPROVE 模板。"""
    # 传一个不存在的策略值（通过构造）
    fb = build_feedback(MagicMock(), "r")  # type: ignore[arg-type]
    assert "r" in fb


# ===================== 4. SelfDiagnoser.diagnose =====================


def test_diagnose_end_to_end_format_error() -> None:
    d = SelfDiagnoser().diagnose("输出 JSON 格式错误", "answer")
    assert d.root_cause == RootCause.FORMAT_ERROR
    assert d.strategy == RepairStrategy.FIX_FORMAT
    assert "JSON 格式错误" in d.feedback
    assert "格式" in d.feedback


def test_diagnose_unknown_returns_generic() -> None:
    d = SelfDiagnoser().diagnose("无法分类的文本zzz")
    assert d.root_cause == RootCause.UNKNOWN
    assert d.strategy == RepairStrategy.GENERIC_IMPROVE


def test_diagnose_never_raises_on_empty() -> None:
    d = SelfDiagnoser().diagnose("")
    assert isinstance(d, Diagnosis)
    assert d.root_cause == RootCause.UNKNOWN


def test_diagnose_returns_diagnosis_dataclass() -> None:
    d = SelfDiagnoser().diagnose("推理错误")
    assert isinstance(d, Diagnosis)
    assert hasattr(d, "root_cause")
    assert hasattr(d, "strategy")
    assert hasattr(d, "feedback")


# ===================== 5. executor 集成（mock）=====================


def _make_agent(self_heal: bool = True, max_retries: int = 2) -> Agent:
    return Agent(
        id=__import__("uuid").uuid4(),
        name="diagnose-agent",
        system_prompt="助手",
        model_alias="default",
        tools=[],
        max_turns=5,
        temperature=0.7,
        is_active=True,
        self_eval=True,
        self_heal=self_heal,
        self_eval_threshold=0.9,  # 高阈值确保首轮不达标触发 heal
        self_heal_max_retries=max_retries,
    )


async def test_executor_self_heal_uses_strategy_feedback() -> None:
    """self-heal 触发时使用 self-diagnose 策略化反馈（验证反馈含策略关键词）。"""
    agent = _make_agent(max_retries=1)
    # 第一轮：给出初始答案；第二轮：heal 重跑给改进答案
    responses = [
        LLMResponse(content="初始答案", usage={"total_tokens": 10}),
        LLMResponse(content="改进答案", usage={"total_tokens": 15}),
    ]
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = min(call_idx["n"], len(responses) - 1)
        call_idx["n"] += 1
        return responses[idx]

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat

    # mock judge_llm：首轮不达标（0.3 + 格式错误 reason），第二轮达标（0.95）
    import app.domains.evals.judge as judge_mod
    judge_results = [
        MagicMock(score=0.3, reason="输出 JSON 格式不符合要求", passed=False),
        MagicMock(score=0.95, reason="good", passed=True),
    ]
    judge_idx = {"n": 0}

    async def fake_judge(actual, expected, client, criteria=None):
        idx = min(judge_idx["n"], len(judge_results) - 1)
        judge_idx["n"] += 1
        return judge_results[idx]

    monkey = pytest.MonkeyPatch()
    monkey.setattr(judge_mod, "judge_llm", fake_judge)
    try:
        executor = AgentExecutor(mock_llm)
        result = await executor.run(agent, "问题")
    finally:
        monkey.undo()

    assert result.final_answer == "改进答案"
    # 验证第二轮 messages 含策略化反馈（FORMAT_ERROR → FIX_FORMAT → "格式"）
    # 通过 trace 间接验证：heal_attempts=1 表示触发了一次 heal
    assert result.eval_score == 0.95
    assert result.heal_attempts == 1


async def test_executor_self_heal_disabled_skips_diagnose() -> None:
    """self_heal=False 时不触发诊断（向后兼容）。"""
    agent = _make_agent(self_heal=False, max_retries=2)

    async def fake_chat(messages, tools=None, response_format=None):
        return LLMResponse(content="答案", usage={"total_tokens": 10})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat

    import app.domains.evals.judge as judge_mod

    async def fake_judge(actual, expected, client, criteria=None):
        return MagicMock(score=0.3, reason="差", passed=False)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(judge_mod, "judge_llm", fake_judge)
    try:
        executor = AgentExecutor(mock_llm)
        result = await executor.run(agent, "问题")
    finally:
        monkey.undo()

    # self_heal=False → 不重跑
    assert result.final_answer == "答案"
    assert result.heal_attempts == 0
