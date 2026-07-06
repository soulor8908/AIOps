"""Agent 配置调优推荐（E1：eval 结果反馈到 agent 配置优化）。

闭环核心：``EvalSample``（生产采样，含 ``agent_id`` + ``judge_score`` +
``judge_reason``）→ 按失败模式聚合 → ``SelfDiagnoser`` 根因分类 →
``AgentConfigRecommendation`` 结构化推荐 → admin 通过 ``PATCH /agents/{id}``
回写。

设计要点：
- **归因到 agent**：``EvalRun`` 本身无 ``agent_id``，但 ``EvalSample`` 有。
  本模块按 ``agent_id`` 聚合样本，绕过 ``run_online_eval`` 的 run 级聚合盲区。
- **复用 SelfDiagnose**：``analyze_root_cause`` 已有 7 类根因分类
  （AMBIGUOUS_QUERY / INCOMPLETE_INFO / TOOL_MISUSE / REASONING_ERROR /
  FORMAT_ERROR / HALLUCINATION / UNKNOWN），直接用于聚合 ``judge_reason``
  统计失败模式分布。
- **零 LLM 成本**：根因分析与推荐生成均为启发式规则，不调用 LLM。
  生产可叠加 LLM 分析（与 SelfDiagnoser 预留的 LLM 注入口一致）。
- **保守推荐**：仅给出建议，不自动回写——admin 审阅后通过 PATCH 端点手动应用，
  避免误调优导致生产 Agent 退化。

推荐策略（根因 → 配置调整建议）：
- ``FORMAT_ERROR`` 高占比 → 建议在 ``system_prompt`` 末尾追加格式约束示例
- ``HALLUCINATION`` 高占比 → 建议降低 ``temperature`` 或开启 ``self_eval``
- ``TOOL_MISUSE`` 高占比 → 建议在 ``system_prompt`` 补充工具使用指引
- ``REASONING_ERROR`` 高占比 → 建议提高 ``max_turns`` 或开启 ``self_heal``
- ``AMBIGUOUS_QUERY`` / ``INCOMPLETE_INFO`` → 建议优化 ``system_prompt``
  添加"先澄清再作答"指令
- 整体低分且未开 self_eval → 建议开启 self_eval + self_heal 闭环
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.agents.models import Agent
from app.domains.agents.self_diagnose import RootCause, analyze_root_cause
from app.domains.evals.models import EvalSample

logger = logging.getLogger("app.agents.tuning")

# 触发推荐的最低失败样本数——少于此数则样本不足，不生成推荐（避免噪声）。
_MIN_SAMPLES_FOR_RECOMMENDATION = 5
# 失败判定阈值：judge_score < 此值视为失败样本。
_FAILURE_SCORE_THRESHOLD = 0.7
# 根因占比阈值：某根因占失败样本比例超此值才触发对应推荐。
_DOMINANT_CAUSE_RATIO = 0.3


@dataclass(slots=True)
class FailurePattern:
    """单类失败模式统计。"""

    root_cause: RootCause
    count: int
    ratio: float
    sample_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentConfigRecommendation:
    """Agent 配置推荐（E1 闭环产物）。

    ``suggested_update`` 为可直接传给 ``AgentUpdate`` 的 dict（admin 审阅后
    通过 ``PATCH /agents/{id}`` 回写）。``rationale`` 解释推荐理由，便于
    admin 决策是否应用。
    """

    agent_id: uuid.UUID
    agent_name: str
    total_samples: int
    failed_samples: int
    avg_score: float
    failure_patterns: list[FailurePattern]
    suggested_update: dict
    rationale: str


async def aggregate_agent_failures(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    sample_limit: int = 100,
) -> tuple[int, int, float, list[EvalSample]]:
    """聚合某 agent 的 eval 样本统计。

    返回 ``(total, failed, avg_score, failed_samples)``：
    - ``total``：该 agent 已 judged 的样本总数
    - ``failed``：其中 ``judge_score < _FAILURE_SCORE_THRESHOLD`` 的数量
    - ``avg_score``：所有 judged 样本的平均 judge_score（无样本则 0.0）
    - ``failed_samples``：失败样本列表（按 priority DESC, sampled_at DESC），
      供根因分析用，最多 ``sample_limit`` 条

    用 ``func.avg`` 在 DB 聚合，避免拉全量样本到内存。
    """
    # 聚合统计
    stats_stmt = (
        select(
            func.count(EvalSample.id).label("total"),
            func.avg(EvalSample.judge_score).label("avg_score"),
        )
        .where(
            EvalSample.agent_id == agent_id,
            EvalSample.judged.is_(True),
        )
    )
    row = (await session.execute(stats_stmt)).one()
    total = int(row.total or 0)
    avg_score = float(row.avg_score) if row.avg_score is not None else 0.0

    # 失败样本（仅取 score < 阈值的，用于根因分析）
    failed_stmt = (
        select(EvalSample)
        .where(
            EvalSample.agent_id == agent_id,
            EvalSample.judged.is_(True),
            EvalSample.judge_score < _FAILURE_SCORE_THRESHOLD,
        )
        .order_by(EvalSample.priority.desc(), EvalSample.sampled_at.desc())
        .limit(sample_limit)
    )
    failed_samples = list((await session.execute(failed_stmt)).scalars().all())
    return total, len(failed_samples), avg_score, failed_samples


def classify_failure_patterns(
    failed_samples: list[EvalSample],
) -> list[FailurePattern]:
    """对失败样本按根因分类，返回按 count 降序的模式列表。

    用 ``SelfDiagnoser.analyze_root_cause`` 对每条 ``judge_reason`` 分类，
    统计各根因的 count / ratio，并保留最多 3 条代表性 reason 供 admin 参考。
    """
    if not failed_samples:
        return []
    cause_counter: Counter[RootCause] = Counter()
    cause_reasons: dict[RootCause, list[str]] = {}
    for sample in failed_samples:
        reason = sample.judge_reason or ""
        cause = analyze_root_cause(reason, sample.actual_output or "")
        cause_counter[cause] += 1
        cause_reasons.setdefault(cause, []).append(reason)

    total = len(failed_samples)
    patterns: list[FailurePattern] = []
    for cause, count in cause_counter.most_common():
        ratio = count / total
        # 保留最多 3 条非空 reason 作为代表样本
        samples = [r for r in cause_reasons[cause] if r][:3]
        patterns.append(
            FailurePattern(
                root_cause=cause,
                count=count,
                ratio=ratio,
                sample_reasons=samples,
            )
        )
    return patterns


def _build_suggestion(
    agent: Agent,
    avg_score: float,
    patterns: list[FailurePattern],
) -> tuple[dict, str]:
    """根据失败模式生成配置推荐 dict + 理由说明。

    策略映射（见模块 docstring）。多条规则可叠加，但每条只设一次（避免覆盖）。
    保守起见：``system_prompt`` 仅追加建议文本（不覆盖原 prompt），由 admin
    最终编辑；``temperature`` / ``max_turns`` / ``self_eval`` 等数值/开关字段
    直接给出建议值。
    """
    suggestion: dict = {}
    rationales: list[str] = []

    # 整体低分 + 未开 self_eval → 建议开启 self_eval + self_heal 闭环
    if avg_score < 0.7 and not agent.self_eval:
        suggestion["self_eval"] = True
        suggestion["self_heal"] = True
        suggestion["self_heal_max_retries"] = max(agent.self_heal_max_retries, 1)
        rationales.append(
            f"平均 judge_score={avg_score:.2f} 低于 0.7 且 self_eval 未开启，"
            "建议开启 self_eval + self_heal 闭环以在执行时自检并重试"
        )

    for pattern in patterns:
        if pattern.ratio < _DOMINANT_CAUSE_RATIO:
            continue
        cause = pattern.root_cause
        cause_desc = f"{cause.value} 占失败样本 {pattern.ratio:.0%}（{pattern.count} 条）"

        if cause == RootCause.FORMAT_ERROR:
            # 已有 system_prompt 则追加格式约束，否则建议设置
            prompt_addition = (
                "\n\n## 输出格式要求\n请严格按指定格式（如 JSON/Markdown）"
                "组织输出，确保字段名与结构符合要求。"
            )
            _append_prompt_suggestion(suggestion, agent, prompt_addition)
            rationales.append(f"{cause_desc}：建议在 system_prompt 追加格式约束")

        elif cause == RootCause.HALLUCINATION:
            # 降低 temperature 抑制幻觉
            new_temp = max(0.0, agent.temperature - 0.2)
            if new_temp < agent.temperature:
                suggestion["temperature"] = new_temp
                rationales.append(
                    f"{cause_desc}：建议降低 temperature "
                    f"{agent.temperature} → {new_temp} 抑制幻觉"
                )

        elif cause == RootCause.TOOL_MISUSE:
            prompt_addition = (
                "\n\n## 工具使用指引\n调用工具前请核对参数名与类型，"
                "调用失败时检查参数后重试，不要跳过工具直接臆测结果。"
            )
            _append_prompt_suggestion(suggestion, agent, prompt_addition)
            rationales.append(f"{cause_desc}：建议在 system_prompt 补充工具使用指引")

        elif cause == RootCause.REASONING_ERROR:
            # 提高 max_turns 给更多推理步骤
            new_turns = min(agent.max_turns + 5, 50)
            if new_turns > agent.max_turns:
                suggestion["max_turns"] = new_turns
                rationales.append(
                    f"{cause_desc}：建议提高 max_turns "
                    f"{agent.max_turns} → {new_turns} 给更多推理步骤"
                )

        elif cause in (RootCause.AMBIGUOUS_QUERY, RootCause.INCOMPLETE_INFO):
            prompt_addition = (
                "\n\n## 澄清策略\n当问题存在歧义或信息不足时，请先明确核心意图"
                "或指出关键缺失信息，再基于已知信息作答。"
            )
            _append_prompt_suggestion(suggestion, agent, prompt_addition)
            rationales.append(f"{cause_desc}：建议在 system_prompt 添加澄清策略指令")

    return suggestion, "；".join(rationales) if rationales else "无显式推荐（失败模式分散，建议人工分析样本）"


def _append_prompt_suggestion(
    suggestion: dict, agent: Agent, addition: str
) -> None:
    """把追加文本合并到 suggestion 的 system_prompt 字段。

    多次追加累加（不覆盖），最终由 admin 审阅编辑。若 suggestion 已有
    system_prompt（来自前一条规则），在其后累加；否则基于 agent 现有 prompt。
    """
    existing = suggestion.get("system_prompt", agent.system_prompt or "")
    suggestion["system_prompt"] = existing + addition


async def recommend_agent_config(
    session: AsyncSession, agent_id: uuid.UUID
) -> AgentConfigRecommendation:
    """生成 agent 配置推荐（E1 闭环核心入口）。

    流程：
    1. 取 agent（不存在抛 NotFoundError）
    2. 聚合该 agent 的 judged 样本统计 + 失败样本
    3. 失败样本不足（< ``_MIN_SAMPLES_FOR_RECOMMENDATION``）→ 返回空推荐
       （rationale 说明样本不足，suggested_update 为空 dict）
    4. ``classify_failure_patterns`` 按根因分类
    5. ``_build_suggestion`` 生成配置推荐 + 理由

    返回的 ``AgentConfigRecommendation`` 由 router 层序列化返回给 admin。
    admin 审阅 ``suggested_update`` 后通过 ``PATCH /agents/{id}`` 回写。
    """
    from app.core.exceptions import NotFoundError

    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} 不存在")

    total, failed, avg_score, failed_samples = await aggregate_agent_failures(
        session, agent_id
    )

    patterns = classify_failure_patterns(failed_samples)

    if failed < _MIN_SAMPLES_FOR_RECOMMENDATION:
        return AgentConfigRecommendation(
            agent_id=agent_id,
            agent_name=agent.name,
            total_samples=total,
            failed_samples=failed,
            avg_score=avg_score,
            failure_patterns=patterns,
            suggested_update={},
            rationale=(
                f"失败样本数 {failed} 不足（阈值 "
                f"{_MIN_SAMPLES_FOR_RECOMMENDATION}），无法生成可靠推荐。"
                "建议积累更多 eval 样本后再分析。"
            ),
        )

    suggestion, rationale = _build_suggestion(agent, avg_score, patterns)
    return AgentConfigRecommendation(
        agent_id=agent_id,
        agent_name=agent.name,
        total_samples=total,
        failed_samples=failed,
        avg_score=avg_score,
        failure_patterns=patterns,
        suggested_update=suggestion,
        rationale=rationale,
    )


__all__ = [
    "AgentConfigRecommendation",
    "FailurePattern",
    "aggregate_agent_failures",
    "classify_failure_patterns",
    "recommend_agent_config",
]
