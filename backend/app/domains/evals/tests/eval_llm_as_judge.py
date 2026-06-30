"""L4 LLM-as-Judge eval — AI 输出质量评估（testing.spec.md§6）。

评估维度：
- Prompt 渲染质量：模板变量替换后输出是否准确、相关
- RAG 检索相关性：检索结果与问题的语义匹配度
- Agent 输出合理性：Agent 回答是否符合系统指令

门槛（testing.spec.md§6.2）：得分 > 0.85。低于门槛不阻断合并但标红评审。

运行条件：
- 需 ``OPENAI_API_KEY`` 环境变量（或 ``ANTHROPIC_API_KEY``）
- 无 key 时自动跳过（``pytest.mark.skipif``）

用法::

    # 跑全部 L4 eval
    pytest app/domains/evals/tests/eval_llm_as_judge.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from app.core.llm_client import LLMClient, LLMConfig, Message, Provider
from app.domains.evals.judge import JudgeResult, judge_llm

# ===================== 跳过条件 =====================

_HAS_LLM_KEY = bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
_L4_REASON = "需要 OPENAI_API_KEY 或 ANTHROPIC_API_KEY 环境变量"

# L4 门槛（testing.spec.md§6.2）
L4_THRESHOLD = 0.85

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _HAS_LLM_KEY, reason=_L4_REASON),
]


def _make_client() -> LLMClient:
    """构造默认 LLMClient（provider 由可用 key 决定）。"""
    if os.getenv("OPENAI_API_KEY"):
        provider: Provider = "openai"
        model = "gpt-4o-mini"
    else:
        provider = "anthropic"
        model = "claude-3-5-sonnet-20241022"
    config = LLMConfig(provider=provider, model=model, api_key="")
    return LLMClient(config)


def _render_prompt(template: str, variables: dict[str, str]) -> str:
    """简单变量替换 {{key}} → value。"""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


async def _generate_answer(prompt: str, client: LLMClient) -> str:
    """调用 LLM 生成回答。"""
    resp = await client.chat([Message(role="user", content=prompt)])
    return resp.content


# ===================== eval cases =====================

PROMPT_QUALITY_CASES: list[dict[str, str]] = [
    {
        "name": "greeting_template",
        "prompt": "用中文向 {{name}} 打招呼，简短一句话。",
        "variables": '{"name": "张三"}',
        "criteria": "输出必须是中文打招呼，包含名字「张三」，且不超过 20 字",
        "expected": "你好，张三！",
    },
    {
        "name": "translation_template",
        "prompt": "将以下英文翻译为中文：{{text}}",
        "variables": '{"text": "Hello, world!"}',
        "criteria": "输出必须是「Hello, world!」的中文翻译，准确无多余解释",
        "expected": "你好，世界！",
    },
    {
        "name": "summary_template",
        "prompt": "用一句话总结：{{content}}",
        "variables": (
            '{"content": "Python 是一种解释型、面向对象的高级编程语言，'
            '由 Guido van Rossum 于 1991 年发布。"}'
        ),
        "criteria": "输出必须是关于 Python 语言的一句话总结，包含关键信息（解释型/面向对象/Guido）",
        "expected": "Python 是 Guido van Rossum 创建的解释型面向对象编程语言。",
    },
]


@pytest.mark.parametrize("case", PROMPT_QUALITY_CASES, ids=lambda c: c["name"])
async def test_prompt_rendering_quality(case: dict[str, str]) -> None:
    """L4: Prompt 模板渲染后 LLM 输出质量评估。

    流程：渲染模板 → LLM 生成 → judge_llm 打分 → 断言 ≥ 0.85。
    """
    variables = json.loads(case["variables"])
    client = _make_client()
    try:
        rendered = _render_prompt(case["prompt"], variables)
        actual = await _generate_answer(rendered, client)
        result: JudgeResult = await judge_llm(
            actual=actual,
            expected=case["expected"],
            client=client,
            criteria=case["criteria"],
        )
        assert result.score >= L4_THRESHOLD, (
            f"[L4 FAIL] case={case['name']} score={result.score:.3f} "
            f"< {L4_THRESHOLD} | reason={result.reason} | actual={actual[:100]}"
        )
    finally:
        await client.close()


async def test_rag_answer_relevance() -> None:
    """L4: RAG 回答相关性 — 给定上下文，LLM 应给出相关回答。"""
    context = "FastAPI 是一个现代、快速的 Web 框架，基于 Python 类型提示。"
    question = "FastAPI 是什么？"
    prompt = f"根据以下上下文回答问题。\n上下文：{context}\n问题：{question}"

    client = _make_client()
    try:
        actual = await _generate_answer(prompt, client)
        result = await judge_llm(
            actual=actual,
            expected="FastAPI 是一个基于 Python 类型提示的现代、快速的 Web 框架。",
            client=client,
            criteria="回答应提及 FastAPI 是 Web 框架、基于 Python、快速或现代",
        )
        assert result.score >= L4_THRESHOLD, (
            f"[L4 FAIL] rag_relevance score={result.score:.3f} < {L4_THRESHOLD} | {result.reason}"
        )
    finally:
        await client.close()


async def test_agent_response_coherence() -> None:
    """L4: Agent 回答连贯性 — 系统指令约束下的回答质量。"""
    system_prompt = "你是一个简洁的助手，回答不超过一句话。"
    user_input = "什么是递归？"

    client = _make_client()
    try:
        resp = await client.chat([
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_input),
        ])
        actual = resp.content
        result = await judge_llm(
            actual=actual,
            expected="递归是函数调用自身的编程技巧，需有终止条件。",
            client=client,
            criteria="回答应解释递归概念（函数调用自身），且简洁（一句话）",
        )
        assert result.score >= L4_THRESHOLD, (
            f"[L4 FAIL] agent_coherence score={result.score:.3f} < {L4_THRESHOLD} | {result.reason}"
        )
    finally:
        await client.close()
