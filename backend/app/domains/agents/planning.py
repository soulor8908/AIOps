"""Planning + Reflection（P2-10）— 执行前 plan + 执行后 reflect。

- ``Planner``：执行前调用 LLM 把 ``user_input`` 分解为有序子任务列表（带建议
  工具），渲染为 system 消息注入到 ReAct 循环的初始 messages，引导 LLM
  按计划逐步执行而非"想到哪做到哪"。
- ``Reflector``：执行后调用 LLM 对照 plan + traces 评估完成度与质量，产出
  结构化反思（覆盖情况 / 优点 / 不足 / 改进建议），存入 ``ExecutionResult``
  供可观测性消费（不触发重跑）。

降级策略：LLM 调用失败或 JSON 解析失败时返回 None，executor 退化为无 plan /
无 reflection 的 ReAct 行为，不阻塞主流程。

与 P3-11 self_eval 的互补关系：
- self_eval 量化打分（score vs threshold）驱动 self-heal 重跑——闭环控制
- reflection 定性总结产出改进建议——开环观测，不触发重跑，仅记录学习

零外部依赖：仅用 stdlib json + LLMClient，与 P1-5 QueryRewriter 同模式。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.llm_client import LLMClient, LLMResponse, Message

logger = logging.getLogger("app.agents.planning")

# plan/reflection 的最大子任务数 / 建议条数，防止 LLM 输出过长污染 context
_MAX_PLAN_STEPS = 8
_MAX_REFLECTION_SUGGESTIONS = 5
# traces 渲染时截断的字符数，避免 reflect prompt 超长
_TRACE_THOUGHT_LIMIT = 200
_TRACE_OBSERVATION_LIMIT = 200
# 反思 prompt 中最终答案的截断字符数
_REFLECT_ANSWER_LIMIT = 500
# reflect 最多取最近 N 个 traces，避免长任务 prompt 超长
_REFLECT_MAX_TRACES = 10


@dataclass(slots=True)
class PlanStep:
    """单个计划步骤。``suggested_tools`` 仅作 LLM 提示，不强制使用。"""

    description: str
    suggested_tools: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Plan:
    """执行计划：goal + 有序步骤。``to_prompt`` 渲染为可注入的 system 消息。"""

    goal: str
    steps: list[PlanStep]

    def to_prompt(self) -> str:
        """渲染为可注入 system 消息的字符串。"""
        lines = [f"执行计划（goal: {self.goal}）："]
        for i, step in enumerate(self.steps, 1):
            tools_hint = (
                f" [建议工具: {', '.join(step.suggested_tools)}]"
                if step.suggested_tools
                else ""
            )
            lines.append(f"{i}. {step.description}{tools_hint}")
        return "\n".join(lines)


@dataclass(slots=True)
class Reflection:
    """执行后反思。``to_summary`` 渲染为简短字符串存入 ExecutionResult。"""

    plan_coverage: str
    strengths: list[str]
    weaknesses: list[str]
    suggestions: list[str]

    def to_summary(self) -> str:
        """渲染为简短字符串存入 ExecutionResult.reflection。"""
        parts = [f"覆盖: {self.plan_coverage}"]
        if self.strengths:
            parts.append(f"优点: {'; '.join(self.strengths)}")
        if self.weaknesses:
            parts.append(f"不足: {'; '.join(self.weaknesses)}")
        if self.suggestions:
            parts.append(f"建议: {'; '.join(self.suggestions)}")
        return " | ".join(parts)


def _strip_code_fence(raw: str) -> str:
    """剥离 ```json ... ``` 代码块包裹，返回裸 JSON 文本。"""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if len(lines) < 2:
        return text
    # ```json\n{...}\n``` → {...}（去掉首尾 ``` 行）
    if lines[-1].startswith("```"):
        return "\n".join(lines[1:-1])
    return "\n".join(lines[1:])


class Planner:
    """执行前规划器：LLM 把 user_input 分解为子任务列表。

    所有异常捕获返回 None，executor 退化为无 plan 的 ReAct 行为。
    """

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def plan(
        self,
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> Plan | None:
        """生成执行计划。LLM 调用或解析失败返回 None。"""
        tool_names = [
            t.get("name", "")
            for t in (tools or [])
            if isinstance(t, dict) and t.get("name")
        ]
        tool_hint = (
            f"可用工具: {', '.join(tool_names)}" if tool_names else "无可用工具"
        )
        sys_msg = system_prompt or "You are a helpful assistant."
        prompt = (
            f"把以下任务分解为最多 {_MAX_PLAN_STEPS} 个有序子任务，"
            f"每步指明建议工具（从可用工具中选，无则留空）。\n"
            f"返回 JSON："
            f'{{"goal": str, "steps": [{{"description": str, "suggested_tools": [str]}}]}}\n'
            f"仅返回 JSON，不要其他文本。\n\n"
            f"任务: {user_input}\n"
            f"{tool_hint}"
        )
        try:
            resp: LLMResponse = await self.llm.chat([
                Message(role="system", content=sys_msg),
                Message(role="user", content=prompt),
            ])
        except Exception:  # noqa: BLE001
            logger.warning("Planner LLM 调用失败，跳过 planning", exc_info=True)
            return None
        return self._parse_plan(resp.content, user_input)

    def _parse_plan(self, raw: str, user_input: str) -> Plan | None:
        """宽容 JSON 解析。失败返回 None。"""
        text = _strip_code_fence(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Planner JSON 解析失败，跳过 planning: %s", raw[:200])
            return None
        if not isinstance(data, dict):
            return None
        goal = str(data.get("goal") or user_input)
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return None
        steps: list[PlanStep] = []
        for s in raw_steps[:_MAX_PLAN_STEPS]:
            if not isinstance(s, dict):
                continue
            desc = str(s.get("description", "")).strip()
            if not desc:
                continue
            tools_field = s.get("suggested_tools", [])
            if not isinstance(tools_field, list):
                tools_field = []
            steps.append(
                PlanStep(
                    description=desc,
                    suggested_tools=[
                        str(t) for t in tools_field if str(t).strip()
                    ],
                )
            )
        if not steps:
            return None
        return Plan(goal=goal, steps=steps)


class Reflector:
    """执行后反思器：LLM 对照 plan + traces 评估完成度与质量。

    所有异常捕获返回 None，不阻塞主流程。``reflect`` 不触发重跑，
    仅产出结构化反思存入 ExecutionResult 供可观测性消费。
    """

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def reflect(
        self,
        user_input: str,
        plan: Plan | None,
        traces: list[Any],
        final_answer: str,
    ) -> Reflection | None:
        """生成反思。LLM 调用或解析失败返回 None。

        ``traces`` 元素期望有 ``turn`` / ``thought`` / ``observation`` 属性
        （ExecutionTrace 满足），但用 getattr 容错以解耦。
        """
        plan_text = plan.to_prompt() if plan else "（无执行计划）"
        # 简化 traces 渲染，避免 LLM 输入过长
        trace_lines: list[str] = []
        for t in traces[:_REFLECT_MAX_TRACES]:
            turn = getattr(t, "turn", "?")
            thought = str(getattr(t, "thought", ""))[:_TRACE_THOUGHT_LIMIT]
            obs = getattr(t, "observation", None)
            obs_str = (
                f" → {str(obs)[:_TRACE_OBSERVATION_LIMIT]}" if obs else ""
            )
            trace_lines.append(f"turn {turn}: {thought}{obs_str}")
        traces_text = "\n".join(trace_lines) or "（无 traces）"
        prompt = (
            f"对照执行计划与 traces，评估完成度并产出结构化反思。\n"
            f"返回 JSON："
            f'{{"plan_coverage": str, "strengths": [str], '
            f'"weaknesses": [str], "suggestions": [str]}}\n'
            f"仅返回 JSON，不要其他文本。\n\n"
            f"原始任务: {user_input}\n"
            f"{plan_text}\n\n"
            f"执行 traces:\n{traces_text}\n\n"
            f"最终答案: {final_answer[:_REFLECT_ANSWER_LIMIT]}"
        )
        try:
            resp: LLMResponse = await self.llm.chat([
                Message(role="system", content="You are a critical reviewer."),
                Message(role="user", content=prompt),
            ])
        except Exception:  # noqa: BLE001
            logger.warning("Reflector LLM 调用失败，跳过 reflection", exc_info=True)
            return None
        return self._parse_reflection(resp.content)

    def _parse_reflection(self, raw: str) -> Reflection | None:
        """宽容 JSON 解析。失败返回 None。"""
        text = _strip_code_fence(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "Reflector JSON 解析失败，跳过 reflection: %s", raw[:200]
            )
            return None
        if not isinstance(data, dict):
            return None

        def _str_list(key: str) -> list[str]:
            v = data.get(key, [])
            if not isinstance(v, list):
                return []
            return [
                str(x).strip()
                for x in v
                if str(x).strip()
            ][:_MAX_REFLECTION_SUGGESTIONS]

        coverage = str(data.get("plan_coverage", "")).strip()
        if not coverage:
            return None
        return Reflection(
            plan_coverage=coverage,
            strengths=_str_list("strengths"),
            weaknesses=_str_list("weaknesses"),
            suggestions=_str_list("suggestions"),
        )


__all__ = [
    "Plan",
    "PlanStep",
    "Planner",
    "Reflection",
    "Reflector",
]
