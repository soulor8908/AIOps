"""Self-diagnose（P1-7）— 失败时先根因分析再选修复策略。

升级原 self-heal（盲目"请改进回答"重跑）为 self-diagnose：
1. ``RootCauseAnalyzer``：分析 eval_reason + answer，归类根因
   （AMBIGUOUS_QUERY / INCOMPLETE_INFO / TOOL_MISUSE / REASONING_ERROR /
    FORMAT_ERROR / HALLUCINATION / UNKNOWN）
2. ``RepairStrategy``：每个根因映射到修复策略
   （CLARIFY_QUERY / REQUEST_MORE_INFO / RETRY_WITH_HINT / DECOMPOSE /
    VERIFY_FACTS / FIX_FORMAT / GENERIC_IMPROVE）
3. ``SelfDiagnoser``：编排 analyze → select → build_feedback，返回结构化
   诊断结果，供 executor 用策略化反馈替换原通用反馈。

设计要点：
- 根因分析用启发式关键词匹配（零 LLM 成本），生产可叠加 LLM 分析。
- 策略库可扩展：新增根因/策略只需改 mapping，不改 executor。
- 所有分析失败降级为 GENERIC_IMPROVE（与原 self-heal 行为一致）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger("app.agents.self_diagnose")


class RootCause(StrEnum):
    """失败根因分类。"""

    AMBIGUOUS_QUERY = "ambiguous_query"
    INCOMPLETE_INFO = "incomplete_info"
    TOOL_MISUSE = "tool_misuse"
    REASONING_ERROR = "reasoning_error"
    FORMAT_ERROR = "format_error"
    HALLUCINATION = "hallucination"
    UNKNOWN = "unknown"


class RepairStrategy(StrEnum):
    """修复策略。"""

    CLARIFY_QUERY = "clarify_query"
    REQUEST_MORE_INFO = "request_more_info"
    RETRY_WITH_HINT = "retry_with_hint"
    DECOMPOSE = "decompose"
    VERIFY_FACTS = "verify_facts"
    FIX_FORMAT = "fix_format"
    GENERIC_IMPROVE = "generic_improve"


# 根因 → 关键词（命中任一即归类）。顺序敏感：先匹配的优先。
_ROOT_CAUSE_KEYWORDS: list[tuple[RootCause, tuple[str, ...]]] = [
    (RootCause.FORMAT_ERROR, ("格式", "format", "json", "结构", "markdown", "语法")),
    (RootCause.HALLUCINATION, ("幻觉", "hallucinat", "捏造", "虚构", "编造", "不存在")),
    (RootCause.TOOL_MISUSE, ("工具", "tool", "参数", "parameter", "调用错误", "API")),
    (RootCause.INCOMPLETE_INFO, ("不完整", "incomplete", "缺少", "missing", "遗漏", "信息不足")),
    (RootCause.AMBIGUOUS_QUERY, ("模糊", "ambiguous", "歧义", "不明确", " unclear")),
    (RootCause.REASONING_ERROR, ("推理", "reason", "逻辑", "logic", "错误", "wrong", "incorrect")),
]

# 根因 → 修复策略
_CAUSE_TO_STRATEGY: dict[RootCause, RepairStrategy] = {
    RootCause.AMBIGUOUS_QUERY: RepairStrategy.CLARIFY_QUERY,
    RootCause.INCOMPLETE_INFO: RepairStrategy.REQUEST_MORE_INFO,
    RootCause.TOOL_MISUSE: RepairStrategy.RETRY_WITH_HINT,
    RootCause.REASONING_ERROR: RepairStrategy.DECOMPOSE,
    RootCause.FORMAT_ERROR: RepairStrategy.FIX_FORMAT,
    RootCause.HALLUCINATION: RepairStrategy.VERIFY_FACTS,
    RootCause.UNKNOWN: RepairStrategy.GENERIC_IMPROVE,
}

# 策略 → 反馈模板（{reason} 占位 eval_reason）
_STRATEGY_FEEDBACK: dict[RepairStrategy, str] = {
    RepairStrategy.CLARIFY_QUERY: (
        "上一轮回答未达标（原因：{reason}）。问题可能存在歧义，"
        "请在回答前先明确问题的核心意图，再针对性作答。"
    ),
    RepairStrategy.REQUEST_MORE_INFO: (
        "上一轮回答未达标（原因：{reason}）。信息可能不完整，"
        "请基于已知信息作答，并明确指出哪些关键信息缺失。"
    ),
    RepairStrategy.RETRY_WITH_HINT: (
        "上一轮回答未达标（原因：{reason}）。可能是工具调用方式有误，"
        "请检查工具参数与调用流程，修正后重试。"
    ),
    RepairStrategy.DECOMPOSE: (
        "上一轮回答未达标（原因：{reason}）。推理过程可能出错，"
        "请将问题分解为多个步骤，逐步推理后给出最终答案。"
    ),
    RepairStrategy.VERIFY_FACTS: (
        "上一轮回答未达标（原因：{reason}）。可能存在虚构内容，"
        "请逐条核对事实，仅基于可验证的信息作答。"
    ),
    RepairStrategy.FIX_FORMAT: (
        "上一轮回答未达标（原因：{reason}）。格式不符合要求，"
        "请按指定格式（如 JSON/Markdown）重新组织输出。"
    ),
    RepairStrategy.GENERIC_IMPROVE: (
        "上一轮回答质量自评不达标（原因：{reason}）。"
        "请改进回答，更准确、相关且完整地回应问题。"
    ),
}


@dataclass(slots=True)
class Diagnosis:
    """诊断结果。"""

    root_cause: RootCause
    strategy: RepairStrategy
    feedback: str


def analyze_root_cause(eval_reason: str, answer: str = "") -> RootCause:
    """启发式根因分析。

    按 _ROOT_CAUSE_KEYWORDS 顺序匹配 eval_reason（小写）+ answer（小写）。
    无命中返回 UNKNOWN。零 LLM 成本。
    """
    text = f"{eval_reason} {answer}".lower()
    for cause, keywords in _ROOT_CAUSE_KEYWORDS:
        if any(kw.lower() in text for kw in keywords):
            return cause
    return RootCause.UNKNOWN


def select_strategy(cause: RootCause) -> RepairStrategy:
    """根因 → 修复策略映射。"""
    return _CAUSE_TO_STRATEGY.get(cause, RepairStrategy.GENERIC_IMPROVE)


def build_feedback(strategy: RepairStrategy, eval_reason: str) -> str:
    """策略 → 反馈消息。"""
    template = _STRATEGY_FEEDBACK.get(strategy, _STRATEGY_FEEDBACK[RepairStrategy.GENERIC_IMPROVE])
    return template.format(reason=eval_reason)


class SelfDiagnoser:
    """编排 analyze → select → build_feedback。

    无状态，可直接实例化或作为静态方法使用。保留类形式便于未来注入
    LLM 分析器（构造时传入 llm_client，analyze 时优先用 LLM，失败降级启发式）。
    """

    def diagnose(self, eval_reason: str, answer: str = "") -> Diagnosis:
        """完整诊断流程。永不抛异常——分析失败降级为 GENERIC_IMPROVE。"""
        try:
            cause = analyze_root_cause(eval_reason, answer)
            strategy = select_strategy(cause)
            feedback = build_feedback(strategy, eval_reason)
        except Exception:  # noqa: BLE001
            logger.exception("P1-7 诊断失败，降级为 GENERIC_IMPROVE")
            cause = RootCause.UNKNOWN
            strategy = RepairStrategy.GENERIC_IMPROVE
            feedback = build_feedback(strategy, eval_reason)
        return Diagnosis(root_cause=cause, strategy=strategy, feedback=feedback)


__all__ = [
    "Diagnosis",
    "RepairStrategy",
    "RootCause",
    "SelfDiagnoser",
    "analyze_root_cause",
    "build_feedback",
    "select_strategy",
]
