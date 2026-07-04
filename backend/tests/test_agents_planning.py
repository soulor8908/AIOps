"""Planning + Reflection（P2-10）测试 — 执行前 plan + 执行后 reflect。

覆盖：
1. **Planner**：LLM 生成 plan / 解析失败降级 / LLM 异常降级 / 步骤数上限
2. **Reflector**：LLM 生成 reflection / 解析失败降级 / LLM 异常降级 / 无 plan 兜底
3. **Plan / Reflection dataclass**：to_prompt / to_summary 渲染
4. **_strip_code_fence**：代码块剥离
5. **executor 集成**：planner 注入 system 消息 / reflector 写入 ExecutionResult /
   planner 失败退化为无 plan / 无 planner 时 plan=None
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.core.llm_client import LLMResponse, Message
from app.domains.agents.executor import AgentExecutor
from app.domains.agents.models import Agent, ExecutionTrace
from app.domains.agents.planning import (
    Plan,
    PlanStep,
    Planner,
    Reflection,
    Reflector,
    _strip_code_fence,
)

# ===================== 1. _strip_code_fence =====================


def test_strip_code_fence_plain_json() -> None:
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_strip_code_fence_json_block() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_plain_block() -> None:
    raw = '```\n{"a": 1}\n```'
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_no_closing_fence_returns_inner() -> None:
    """无闭合 ``` 时去掉首行 ```，保留剩余（宽容解析）。"""
    raw = '```\n{"a": 1}'
    assert _strip_code_fence(raw) == '{"a": 1}'


# ===================== 2. Plan / Reflection dataclass =====================


def test_plan_to_prompt_renders_steps() -> None:
    plan = Plan(
        goal="完成报告",
        steps=[
            PlanStep(description="收集资料", suggested_tools=["search"]),
            PlanStep(description="撰写摘要", suggested_tools=[]),
        ],
    )
    text = plan.to_prompt()
    assert "完成报告" in text
    assert "1. 收集资料" in text
    assert "建议工具: search" in text
    assert "2. 撰写摘要" in text


def test_plan_to_prompt_no_tools_hint_when_empty() -> None:
    plan = Plan(goal="g", steps=[PlanStep(description="step1")])
    text = plan.to_prompt()
    assert "建议工具" not in text


def test_reflection_to_summary_full() -> None:
    r = Reflection(
        plan_coverage="全部完成",
        strengths=["逻辑清晰"],
        weaknesses=["缺少数据"],
        suggestions=["补充图表"],
    )
    text = r.to_summary()
    assert "覆盖: 全部完成" in text
    assert "优点: 逻辑清晰" in text
    assert "不足: 缺少数据" in text
    assert "建议: 补充图表" in text


def test_reflection_to_summary_only_coverage() -> None:
    r = Reflection(plan_coverage="部分完成", strengths=[], weaknesses=[], suggestions=[])
    text = r.to_summary()
    assert text == "覆盖: 部分完成"


# ===================== 3. Planner =====================


def _mock_llm_with_response(content: str) -> MagicMock:
    """构造 LLMClient mock，chat 返回固定 content。"""
    mock = MagicMock()
    mock.response_format = None

    async def fake_chat(messages, tools=None, response_format=None):
        return LLMResponse(content=content, usage={"total_tokens": 10})

    mock.chat = fake_chat
    return mock


def _mock_llm_raising(exc: Exception) -> MagicMock:
    """构造 LLMClient mock，chat 抛异常。"""
    mock = MagicMock()

    async def fake_chat(messages, tools=None, response_format=None):
        raise exc

    mock.chat = fake_chat
    return mock


async def test_planner_plan_success() -> None:
    payload = {
        "goal": "调研 X",
        "steps": [
            {"description": "搜索资料", "suggested_tools": ["search"]},
            {"description": "整理结论", "suggested_tools": []},
        ],
    }
    llm = _mock_llm_with_response(json.dumps(payload, ensure_ascii=False))
    planner = Planner(llm)
    plan = await planner.plan("调研 X 的现状")
    assert plan is not None
    assert plan.goal == "调研 X"
    assert len(plan.steps) == 2
    assert plan.steps[0].description == "搜索资料"
    assert plan.steps[0].suggested_tools == ["search"]
    assert plan.steps[1].suggested_tools == []


async def test_planner_plan_success_with_code_fence() -> None:
    payload = {"goal": "g", "steps": [{"description": "s1"}]}
    raw = f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    llm = _mock_llm_with_response(raw)
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is not None
    assert plan.steps[0].description == "s1"


async def test_planner_plan_llm_exception_returns_none() -> None:
    llm = _mock_llm_raising(RuntimeError("llm down"))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is None


async def test_planner_plan_invalid_json_returns_none() -> None:
    llm = _mock_llm_with_response("not a json at all")
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is None


async def test_planner_plan_missing_steps_returns_none() -> None:
    llm = _mock_llm_with_response(json.dumps({"goal": "g"}))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is None


async def test_planner_plan_empty_steps_returns_none() -> None:
    llm = _mock_llm_with_response(json.dumps({"goal": "g", "steps": []}))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is None


async def test_planner_plan_steps_not_list_returns_none() -> None:
    llm = _mock_llm_with_response(json.dumps({"goal": "g", "steps": "not a list"}))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is None


async def test_planner_plan_truncates_to_max_steps() -> None:
    """超过 _MAX_PLAN_STEPS 的步骤被截断。"""
    from app.domains.agents.planning import _MAX_PLAN_STEPS

    steps = [{"description": f"step {i}"} for i in range(_MAX_PLAN_STEPS + 5)]
    payload = {"goal": "g", "steps": steps}
    llm = _mock_llm_with_response(json.dumps(payload))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is not None
    assert len(plan.steps) == _MAX_PLAN_STEPS


async def test_planner_plan_skips_invalid_step_entries() -> None:
    """steps 中非 dict / 无 description 的条目被跳过。"""
    payload = {
        "goal": "g",
        "steps": [
            {"description": "valid"},
            "not a dict",
            {"description": ""},  # 空描述跳过
            {"no_desc": "x"},  # 无 description 跳过
            {"description": "valid2", "suggested_tools": "not a list"},  # tools 非 list 容错
        ],
    }
    llm = _mock_llm_with_response(json.dumps(payload))
    planner = Planner(llm)
    plan = await planner.plan("task")
    assert plan is not None
    assert len(plan.steps) == 2
    assert plan.steps[0].description == "valid"
    assert plan.steps[1].description == "valid2"
    # tools 非 list 时容错为空 list
    assert plan.steps[1].suggested_tools == []


async def test_planner_plan_falls_back_to_user_input_when_goal_missing() -> None:
    payload = {"steps": [{"description": "s1"}]}
    llm = _mock_llm_with_response(json.dumps(payload))
    planner = Planner(llm)
    plan = await planner.plan("my task")
    assert plan is not None
    assert plan.goal == "my task"


async def test_planner_plan_passes_tools_and_system_prompt_to_llm() -> None:
    """planner 把 tools / system_prompt 拼到 prompt 中（通过捕获 messages 验证）。"""
    captured: list[list[Message]] = []

    async def fake_chat(messages, tools=None, response_format=None):
        captured.append(messages)
        return LLMResponse(
            content=json.dumps({"goal": "g", "steps": [{"description": "s"}]}),
            usage={"total_tokens": 5},
        )

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    planner = Planner(mock_llm)
    await planner.plan(
        "task",
        tools=[{"name": "search"}, {"name": "calc"}],
        system_prompt="custom system",
    )
    assert len(captured) == 1
    msgs = captured[0]
    # system 消息含 custom system
    assert any("custom system" in m.content for m in msgs)
    # user 消息含 tool 名
    assert any("search" in m.content and "calc" in m.content for m in msgs)


# ===================== 4. Reflector =====================


async def test_reflector_reflect_success() -> None:
    payload = {
        "plan_coverage": "全部完成",
        "strengths": ["逻辑清晰"],
        "weaknesses": ["缺少数据"],
        "suggestions": ["补充图表"],
    }
    llm = _mock_llm_with_response(json.dumps(payload, ensure_ascii=False))
    reflector = Reflector(llm)
    traces = [ExecutionTrace(turn=1, thought="思考", observation="结果", tokens=10)]
    reflection = await reflector.reflect("task", plan=None, traces=traces, final_answer="answer")
    assert reflection is not None
    assert reflection.plan_coverage == "全部完成"
    assert reflection.strengths == ["逻辑清晰"]
    assert reflection.weaknesses == ["缺少数据"]
    assert reflection.suggestions == ["补充图表"]


async def test_reflector_reflect_with_plan() -> None:
    """传 Plan 时 reflect prompt 中含 plan.to_prompt() 文本。"""
    captured: list[list[Message]] = []

    async def fake_chat(messages, tools=None, response_format=None):
        captured.append(messages)
        payload = {
            "plan_coverage": "ok",
            "strengths": [],
            "weaknesses": [],
            "suggestions": [],
        }
        return LLMResponse(content=json.dumps(payload), usage={"total_tokens": 5})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    reflector = Reflector(mock_llm)
    plan = Plan(goal="g", steps=[PlanStep(description="step1")])
    await reflector.reflect("task", plan=plan, traces=[], final_answer="ans")
    msgs = captured[0]
    # user 消息中应包含 plan.to_prompt() 渲染的 "执行计划"
    assert any("执行计划" in m.content for m in msgs)


async def test_reflector_reflect_llm_exception_returns_none() -> None:
    llm = _mock_llm_raising(RuntimeError("down"))
    reflector = Reflector(llm)
    reflection = await reflector.reflect("task", plan=None, traces=[], final_answer="ans")
    assert reflection is None


async def test_reflector_reflect_invalid_json_returns_none() -> None:
    llm = _mock_llm_with_response("not json")
    reflector = Reflector(llm)
    reflection = await reflector.reflect("task", plan=None, traces=[], final_answer="ans")
    assert reflection is None


async def test_reflector_reflect_missing_coverage_returns_none() -> None:
    payload = {"strengths": ["s"], "weaknesses": [], "suggestions": []}
    llm = _mock_llm_with_response(json.dumps(payload))
    reflector = Reflector(llm)
    reflection = await reflector.reflect("task", plan=None, traces=[], final_answer="ans")
    assert reflection is None


async def test_reflector_reflect_truncates_suggestions() -> None:
    """suggestions 列表被截断到 _MAX_REFLECTION_SUGGESTIONS。"""
    from app.domains.agents.planning import _MAX_REFLECTION_SUGGESTIONS

    payload = {
        "plan_coverage": "ok",
        "strengths": [f"s{i}" for i in range(10)],
        "weaknesses": [f"w{i}" for i in range(10)],
        "suggestions": [f"sg{i}" for i in range(10)],
    }
    llm = _mock_llm_with_response(json.dumps(payload))
    reflector = Reflector(llm)
    reflection = await reflector.reflect("task", plan=None, traces=[], final_answer="ans")
    assert reflection is not None
    assert len(reflection.strengths) == _MAX_REFLECTION_SUGGESTIONS
    assert len(reflection.weaknesses) == _MAX_REFLECTION_SUGGESTIONS
    assert len(reflection.suggestions) == _MAX_REFLECTION_SUGGESTIONS


# ===================== 5. executor 集成 =====================


def _make_agent() -> Agent:
    import uuid as _uuid
    return Agent(
        id=_uuid.uuid4(),
        name="plan-agent",
        system_prompt="助手",
        model_alias="default",
        tools=[],
        max_turns=5,
        temperature=0.7,
        is_active=True,
    )


async def test_executor_with_planner_injects_plan() -> None:
    """planner 返回有效 plan 时，executor 把 plan 注入 messages。"""
    agent = _make_agent()
    plan_payload = {"goal": "g", "steps": [{"description": "s1"}]}
    # planner 调用返回 plan JSON；ReAct 调用返回最终答案
    plan_resp = LLMResponse(content=json.dumps(plan_payload), usage={"total_tokens": 5})
    answer_resp = LLMResponse(content="最终答案", usage={"total_tokens": 10})
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = call_idx["n"]
        call_idx["n"] += 1
        # 第一次调用是 planner
        if idx == 0:
            return plan_resp
        return answer_resp

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    planner = Planner(mock_llm)
    executor = AgentExecutor(mock_llm, planner=planner)
    result = await executor.run(agent, "问题")
    assert result.plan is not None
    assert "执行计划" in result.plan
    assert "s1" in result.plan
    assert result.final_answer == "最终答案"


async def test_executor_with_planner_failure_degrades_to_no_plan() -> None:
    """planner LLM 异常时 executor 退化为无 plan（plan=None），不阻塞主流程。"""
    agent = _make_agent()
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            raise RuntimeError("planner LLM down")
        return LLMResponse(content="答案", usage={"total_tokens": 10})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    planner = Planner(mock_llm)
    executor = AgentExecutor(mock_llm, planner=planner)
    result = await executor.run(agent, "问题")
    assert result.plan is None
    assert result.final_answer == "答案"


async def test_executor_with_reflector_stores_reflection() -> None:
    """reflector 返回有效 reflection 时，ExecutionResult.reflection 非空。"""
    agent = _make_agent()
    reflect_payload = {
        "plan_coverage": "完成",
        "strengths": ["good"],
        "weaknesses": [],
        "suggestions": [],
    }
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            # ReAct 最终答案
            return LLMResponse(content="答案", usage={"total_tokens": 10})
        # reflector 调用
        return LLMResponse(content=json.dumps(reflect_payload), usage={"total_tokens": 5})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    reflector = Reflector(mock_llm)
    executor = AgentExecutor(mock_llm, reflector=reflector)
    result = await executor.run(agent, "问题")
    assert result.reflection is not None
    assert "完成" in result.reflection
    assert "good" in result.reflection


async def test_executor_with_reflector_failure_degrades_to_no_reflection() -> None:
    """reflector LLM 异常时 ExecutionResult.reflection=None，不阻塞主流程。"""
    agent = _make_agent()
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            return LLMResponse(content="答案", usage={"total_tokens": 10})
        raise RuntimeError("reflector down")

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    reflector = Reflector(mock_llm)
    executor = AgentExecutor(mock_llm, reflector=reflector)
    result = await executor.run(agent, "问题")
    assert result.reflection is None
    assert result.final_answer == "答案"


async def test_executor_without_planner_reflector_returns_none() -> None:
    """无 planner / reflector 时 ExecutionResult.plan / reflection 均为 None。"""
    agent = _make_agent()

    async def fake_chat(messages, tools=None, response_format=None):
        return LLMResponse(content="答案", usage={"total_tokens": 10})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    executor = AgentExecutor(mock_llm)
    result = await executor.run(agent, "问题")
    assert result.plan is None
    assert result.reflection is None
    assert result.final_answer == "答案"


async def test_executor_plan_and_reflect_work_together() -> None:
    """planner + reflector 同时启用：plan 注入 messages，reflect 评估最终答案。"""
    agent = _make_agent()
    plan_payload = {"goal": "g", "steps": [{"description": "s1"}, {"description": "s2"}]}
    reflect_payload = {
        "plan_coverage": "全部完成",
        "strengths": ["按计划执行"],
        "weaknesses": [],
        "suggestions": [],
    }
    call_idx = {"n": 0}

    async def fake_chat(messages, tools=None, response_format=None):
        idx = call_idx["n"]
        call_idx["n"] += 1
        if idx == 0:
            return LLMResponse(content=json.dumps(plan_payload), usage={"total_tokens": 5})
        if idx == 1:
            return LLMResponse(content="最终答案", usage={"total_tokens": 10})
        return LLMResponse(content=json.dumps(reflect_payload), usage={"total_tokens": 5})

    mock_llm = MagicMock()
    mock_llm.chat = fake_chat
    planner = Planner(mock_llm)
    reflector = Reflector(mock_llm)
    executor = AgentExecutor(mock_llm, planner=planner, reflector=reflector)
    result = await executor.run(agent, "问题")
    assert result.plan is not None
    assert "s1" in result.plan and "s2" in result.plan
    assert result.reflection is not None
    assert "全部完成" in result.reflection
    assert result.final_answer == "最终答案"
