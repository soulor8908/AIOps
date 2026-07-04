"""core/llm_client.py 单元测试 — LLM 客户端。

使用 httpx.MockTransport mock HTTP 调用，不依赖真实 API。

覆盖：
- LLMConfig 默认值与 temperature 校验
- Message 模型序列化
- LLMClient.chat: openai / anthropic / local 三种 provider
- latency_ms 记录
- HTTP 错误时抛 LLMError
- parse_tool_calls_json 解析
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import httpx
import pytest

from app.core.exceptions import LLMError
from app.core.llm_client import (
    LLMClient,
    LLMConfig,
    LLMResponse,
    Message,
    ToolDef,
    _assemble_tool_calls,
    parse_tool_calls_json,
)

# ===================== 辅助函数 =====================

def _make_client(
    config: LLMConfig, handler: Any, max_retries: int = 0
) -> LLMClient:
    """构造使用 MockTransport 的 LLMClient（默认 max_retries=0 避免单测慢重试）。"""
    transport = httpx.MockTransport(handler)
    client = LLMClient(config, max_retries=max_retries)
    # 替换内部 httpx.AsyncClient 为带 MockTransport 的实例
    client._http = httpx.AsyncClient(transport=transport)
    return client


# ===================== LLMConfig =====================

def test_llm_config_defaults() -> None:
    """默认配置值。"""
    config = LLMConfig(provider="openai", model="gpt-4o")
    assert config.provider == "openai"
    assert config.model == "gpt-4o"
    assert config.api_key == ""
    assert config.base_url == ""
    assert config.temperature == 0.7
    assert config.max_tokens == 1024


def test_llm_config_validation() -> None:
    """temperature 超范围验证。"""
    # 合法温度
    cfg_low = LLMConfig(provider="openai", model="gpt-4o", temperature=0.0)
    assert cfg_low.temperature == 0.0
    cfg_high = LLMConfig(provider="openai", model="gpt-4o", temperature=2.0)
    assert cfg_high.temperature == 2.0

    # 温度过高 → ValueError
    with pytest.raises(ValueError):
        LLMConfig(provider="openai", model="gpt-4o", temperature=3.0)

    # 温度为负 → ValueError
    with pytest.raises(ValueError):
        LLMConfig(provider="openai", model="gpt-4o", temperature=-0.1)


def test_llm_config_all_providers() -> None:
    """三个 provider 均可创建配置。"""
    for p in ("openai", "anthropic", "local"):
        cfg = LLMConfig(provider=p, model="m")  # type: ignore[arg-type]
        assert cfg.provider == p


# ===================== Message =====================

def test_message_model() -> None:
    """Message 模型序列化。"""
    msg = Message(role="user", content="hello world")
    assert msg.role == "user"
    assert msg.content == "hello world"

    d = asdict(msg)
    assert d == {"role": "user", "content": "hello world", "cache_control": False}

    # 不同 role
    for role in ("system", "user", "assistant", "tool"):
        m = Message(role=role, content=f"msg-{role}")
        assert m.role == role


# ===================== LLMResponse =====================

def test_llm_response_defaults() -> None:
    """LLMResponse 默认值。"""
    resp = LLMResponse(content="answer")
    assert resp.content == "answer"
    assert resp.tool_calls == []
    assert resp.usage == {}
    assert resp.raw == {}
    assert resp.latency_ms == 0.0


# ===================== LLMClient — OpenAI =====================

async def test_llm_client_chat_openai() -> None:
    """mock OpenAI API 调用，验证请求和响应。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Hello!", "tool_calls": []}}],
            "usage": {"total_tokens": 42},
        })

    config = LLMConfig(
        provider="openai", model="gpt-4o", api_key="test-key-openai"
    )
    client = _make_client(config, handler)
    try:
        response = await client.chat([
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Say hello"),
        ])
    finally:
        await client.close()

    # 验证请求
    assert "/chat/completions" in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer test-key-openai"
    assert captured["body"]["model"] == "gpt-4o"
    assert captured["body"]["temperature"] == 0.7
    assert len(captured["body"]["messages"]) == 2
    assert captured["body"]["messages"][0]["role"] == "system"

    # 验证响应
    assert response.content == "Hello!"
    assert response.usage == {"total_tokens": 42}
    assert response.tool_calls == []


async def test_llm_client_chat_openai_with_tool_calls() -> None:
    """OpenAI 响应包含原生 tool_calls 时正确解析为 ToolCall（P0-2）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"q": "test"}',
                    },
                }],
            }}],
            "usage": {},
        })

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler)
    try:
        response = await client.chat([Message(role="user", content="search")])
    finally:
        await client.close()

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "search"
    assert response.tool_calls[0].args == {"q": "test"}


async def test_llm_client_chat_openai_custom_base_url() -> None:
    """自定义 base_url 时请求发到自定义地址。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    config = LLMConfig(
        provider="openai", model="gpt-4o", api_key="k",
        base_url="https://my-proxy.example.com/v1",
    )
    client = _make_client(config, handler)
    try:
        await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert captured["url"].startswith("https://my-proxy.example.com/v1/chat/completions")


# ===================== LLMClient — Anthropic =====================

async def test_llm_client_chat_anthropic() -> None:
    """mock Anthropic API 调用（注意 system 消息单独处理）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "Bonjour!"},
            ],
            "usage": {"total_tokens": 15},
        })

    config = LLMConfig(
        provider="anthropic", model="claude-3-5-sonnet-20241022",
        api_key="test-key-anthropic",
    )
    client = _make_client(config, handler)
    try:
        response = await client.chat([
            Message(role="system", content="You are a translator."),
            Message(role="user", content="Translate hello"),
        ])
    finally:
        await client.close()

    # system 消息应被抽离到顶层（P2-10：Anthropic system 为 list[dict] 格式）
    assert captured["body"]["system"] == [
        {"type": "text", "text": "You are a translator."}
    ]
    # messages 中不含 system
    assert all(m["role"] != "system" for m in captured["body"]["messages"])
    assert len(captured["body"]["messages"]) == 1
    # temperature 应被发送（Anthropic 范围 0-1，默认 0.7 透传）
    assert captured["body"]["temperature"] == 0.7

    # 请求头
    assert captured["headers"]["x-api-key"] == "test-key-anthropic"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"

    # 响应文本拼接
    assert response.content == "Bonjour!"
    assert response.usage == {"total_tokens": 15}


async def test_llm_client_chat_anthropic_multiple_system() -> None:
    """Anthropic: 多条 system 消息合并。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
        })

    config = LLMConfig(provider="anthropic", model="claude", api_key="k")
    client = _make_client(config, handler)
    try:
        await client.chat([
            Message(role="system", content="Rule 1."),
            Message(role="system", content="Rule 2."),
            Message(role="user", content="go"),
        ])
    finally:
        await client.close()

    # P2-10：system 为 list[dict]，多条 system 各成一个 block
    system_field = captured["body"]["system"]
    assert isinstance(system_field, list)
    texts = [block["text"] for block in system_field]
    assert "Rule 1." in texts
    assert "Rule 2." in texts


async def test_llm_client_chat_anthropic_temperature_clamp() -> None:
    """Anthropic temperature 上限 1.0，LLMConfig 允许 0-2 时需 clamp。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
        })

    # LLMConfig 接受 temperature=1.5，Anthropic 应 clamp 到 1.0
    config = LLMConfig(provider="anthropic", model="claude", api_key="k", temperature=1.5)
    client = _make_client(config, handler)
    try:
        await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert captured["body"]["temperature"] == 1.0


# ===================== LLMClient — Local =====================

async def test_llm_client_chat_local() -> None:
    """mock local API 调用。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Local reply"}}],
        })

    config = LLMConfig(provider="local", model="llama3", base_url="http://localhost:11434/v1")
    client = _make_client(config, handler)
    try:
        response = await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert "/chat/completions" in captured["url"]
    assert "localhost:11434" in captured["url"]
    # local 不发 Authorization 头
    assert "authorization" not in {k.lower() for k in captured["headers"]}
    assert response.content == "Local reply"


async def test_llm_client_chat_local_default_url() -> None:
    """local provider 不设 base_url 时使用默认 URL。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    config = LLMConfig(provider="local", model="llama3")
    client = _make_client(config, handler)
    try:
        await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert "localhost:11434" in captured["url"]


# ===================== latency =====================

async def test_llm_client_chat_records_latency() -> None:
    """验证 latency_ms 被记录。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "fast"}}],
            "usage": {"total_tokens": 5},
        })

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler)
    try:
        response = await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert response.content == "fast"
    assert response.latency_ms > 0
    assert response.latency_ms < 10000  # 合理上限


# ===================== HTTP error =====================

async def test_llm_client_chat_raises_on_http_error() -> None:
    """HTTP 错误时抛 LLMError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler)
    try:
        with pytest.raises(LLMError):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()


async def test_llm_client_chat_unsupported_provider() -> None:
    """不支持的 provider 抛 LLMError。"""
    config = LLMConfig(provider="openai", model="gpt-4o")  # type: ignore[arg-type]
    client = LLMClient(config)
    # 篡改 provider 为不支持的值
    client.config.provider = "unknown"  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError, match="不支持"):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()


async def test_llm_client_chat_raises_on_connect_error() -> None:
    """连接错误时抛 LLMError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler)
    try:
        with pytest.raises(LLMError):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()


async def test_llm_client_records_error_metric_on_non_retryable_failure() -> None:
    """不可重试失败（4xx）记录 llm_errors{model,"non_retryable"}。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        config = LLMConfig(provider="openai", model="gpt-4o", api_key="bad")
        client = _make_client(config, handler, max_retries=2)
        try:
            with pytest.raises(LLMError):
                await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        assert metrics.get_counter("llm_errors", ("gpt-4o", "non_retryable")) == 1.0
    finally:
        metrics.reset()


async def test_llm_client_records_error_metric_on_retry_exhausted() -> None:
    """可重试故障耗尽后记录 llm_errors{model,"retryable_exhausted"}。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(503, text="Service Unavailable")

        config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
        client = _make_client(config, handler, max_retries=2)
        try:
            with pytest.raises(LLMError):
                await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        # max_retries=2 → 3 次尝试（初始 + 2 次重试）
        assert call_count == 3
        assert metrics.get_counter("llm_errors", ("gpt-4o", "retryable_exhausted")) == 1.0
    finally:
        metrics.reset()


async def test_llm_client_records_error_metric_on_unsupported_provider() -> None:
    """不支持的 provider 记录 llm_errors{model,"unsupported_provider"}。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        config = LLMConfig(provider="openai", model="gpt-4o")
        client = LLMClient(config)
        client.config.provider = "unknown"  # type: ignore[assignment]
        try:
            with pytest.raises(LLMError, match="不支持"):
                await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        assert metrics.get_counter("llm_errors", ("gpt-4o", "unsupported_provider")) == 1.0
    finally:
        metrics.reset()


# ===================== parse_tool_calls_json =====================

def test_parse_tool_calls_valid() -> None:
    """解析有效的工具调用格式。"""
    content = (
        'some text\n```tool_calls\n'
        '[{"name": "search", "args": {"q": "hello"}}]\n'
        '```\nmore text'
    )
    result = parse_tool_calls_json(content)
    assert len(result) == 1
    assert result[0]["name"] == "search"
    assert result[0]["args"] == {"q": "hello"}


def test_parse_tool_calls_multiple() -> None:
    """解析多个工具调用。"""
    content = '```tool_calls\n[{"name": "a", "args": {}}, {"name": "b", "args": {"x": 1}}]\n```'
    result = parse_tool_calls_json(content)
    assert len(result) == 2
    assert result[0]["name"] == "a"
    assert result[1]["name"] == "b"


def test_parse_tool_calls_single_object() -> None:
    """单个 JSON 对象（非数组）也能解析。"""
    content = '```tool_calls\n{"name": "calc", "args": {"expr": "1+1"}}\n```'
    result = parse_tool_calls_json(content)
    assert len(result) == 1
    assert result[0]["name"] == "calc"


def test_parse_tool_calls_no_marker() -> None:
    """无 tool_calls 标记时返回空列表。"""
    assert parse_tool_calls_json("just a normal response") == []
    assert parse_tool_calls_json("") == []


def test_parse_tool_calls_invalid_json() -> None:
    """无效 JSON 时返回空列表。"""
    content = '```tool_calls\n{invalid json}\n```'
    assert parse_tool_calls_json(content) == []


def test_parse_tool_calls_no_closing_marker() -> None:
    """缺少结束标记时也能解析到文件末尾。"""
    content = '```tool_calls\n[{"name": "x", "args": {}}]'
    result = parse_tool_calls_json(content)
    assert len(result) == 1
    assert result[0]["name"] == "x"


# ===================== 重试 + 异常包装 =====================

async def test_llm_client_retries_on_429() -> None:
    """429 限流应重试，最终成功。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok after retry"}}],
        })

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=3)
    try:
        response = await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    assert call_count["n"] == 3
    assert response.content == "ok after retry"


async def test_llm_client_retries_exhausted_raises_llm_error() -> None:
    """持续 500 重试耗尽后抛 LLMError（_RetryableLLMError 的子类）。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500, text="server error")

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=2)
    try:
        with pytest.raises(LLMError):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    # 1 次初始 + 2 次重试 = 3 次
    assert call_count["n"] == 3


async def test_llm_client_no_retry_on_400() -> None:
    """400 鉴权/参数错不可重试，立即抛 LLMError。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(401, text="unauthorized")

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="bad")
    client = _make_client(config, handler, max_retries=3)
    try:
        with pytest.raises(LLMError, match="401"):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()

    # 不可重试 → 只调用 1 次
    assert call_count["n"] == 1


async def test_llm_client_wraps_malformed_response() -> None:
    """响应 200 但结构异常（缺 choices）应抛 LLMError 而非 KeyError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "structure"})

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        with pytest.raises(LLMError, match="响应结构异常"):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()


async def test_llm_client_wraps_json_decode_error() -> None:
    """响应非 JSON 时应抛 LLMError 而非 JSONDecodeError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all", headers={"content-type": "text/plain"})

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        with pytest.raises(LLMError):
            await client.chat([Message(role="user", content="hi")])
    finally:
        await client.close()


async def test_llm_client_async_context_manager() -> None:
    """async with 语法自动关闭连接。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ctx"}}],
        })

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    async with client:
        response = await client.chat([Message(role="user", content="hi")])
    assert response.content == "ctx"
    # 退出 async with 后 httpx client 应已关闭
    assert client._http.is_closed


def test_llm_config_rejects_zero_max_tokens() -> None:
    """max_tokens <= 0 应拒绝。"""
    with pytest.raises(ValueError, match="max_tokens"):
        LLMConfig(provider="openai", model="gpt-4o", max_tokens=0)


# ===================== 指标采集（observability.spec.md§5.1） =====================

async def test_llm_client_records_token_and_cost_metrics() -> None:
    """成功调用后记录 llm_tokens（in/out）与 llm_cost 到 metrics。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            })

        # 配置单价：输入 $0.01/1k，输出 $0.03/1k
        config = LLMConfig(
            provider="openai", model="gpt-4o-test", api_key="k",
            cost_per_1k_input=0.01, cost_per_1k_output=0.03,
        )
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        # 验证 llm_tokens
        assert metrics.get_counter("llm_tokens", ("gpt-4o-test", "in")) == 100
        assert metrics.get_counter("llm_tokens", ("gpt-4o-test", "out")) == 50
        # 验证 llm_cost = 100/1000*0.01 + 50/1000*0.03 = 0.001 + 0.0015 = 0.0025
        cost = metrics.get_counter("llm_cost", ("gpt-4o-test",))
        assert abs(cost - 0.0025) < 1e-9
    finally:
        metrics.reset()


async def test_llm_client_anthropic_usage_field_names() -> None:
    """Anthropic usage 字段名 input_tokens/output_tokens 也能解析。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 80, "output_tokens": 20},
            })

        config = LLMConfig(provider="anthropic", model="claude-test", api_key="k")
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        assert metrics.get_counter("llm_tokens", ("claude-test", "in")) == 80
        assert metrics.get_counter("llm_tokens", ("claude-test", "out")) == 20
    finally:
        metrics.reset()


# ===================== P2-9 成功调用计数 llm_calls =====================

async def test_llm_client_records_llm_call_on_success() -> None:
    """成功调用后记录 llm_calls{model}（P2-9）。"""
    from app.core.llm_client import close_all_clients
    from app.core.metrics import metrics

    metrics.reset()
    # 清空单例缓存，避免其他测试残留干扰
    await close_all_clients()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"total_tokens": 5},
            })

        config = LLMConfig(provider="openai", model="gpt-4o-health", api_key="k")
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        assert metrics.get_counter("llm_calls", ("gpt-4o-health",)) == 1.0
    finally:
        metrics.reset()
        await close_all_clients()


# ===================== P1-5 连接池单例 =====================

async def test_get_llm_client_singleton_by_config() -> None:
    """相同 config 关键字段返回同一实例，不同字段返回不同实例（P1-5）。"""
    from app.core.llm_client import close_all_clients, get_llm_client

    await close_all_clients()
    try:
        cfg1 = LLMConfig(provider="openai", model="gpt-4o", api_key="k1")
        cfg2 = LLMConfig(provider="openai", model="gpt-4o", api_key="k1")  # 同 key
        cfg3 = LLMConfig(provider="openai", model="gpt-4o", api_key="k2")  # 不同 api_key
        cfg4 = LLMConfig(provider="anthropic", model="gpt-4o", api_key="k1")  # 不同 provider

        c1 = get_llm_client(cfg1)
        c2 = get_llm_client(cfg2)
        c3 = get_llm_client(cfg3)
        c4 = get_llm_client(cfg4)

        assert c1 is c2  # 相同关键字段 → 同一实例
        assert c1 is not c3
        assert c1 is not c4
        assert c3 is not c4
    finally:
        await close_all_clients()


async def test_close_all_clients_releases_instances() -> None:
    """close_all_clients 释放所有缓存实例并清空缓存（P1-5）。"""
    from app.core.llm_client import _clients, close_all_clients, get_llm_client

    await close_all_clients()
    try:
        cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
        c1 = get_llm_client(cfg)
        assert len(_clients) == 1

        await close_all_clients()
        assert len(_clients) == 0

        # 再次获取应是新实例
        c2 = get_llm_client(cfg)
        assert c1 is not c2
    finally:
        await close_all_clients()


# ===================== Streaming（P0-1） =====================


def _sse_lines(events: list[str]) -> bytes:
    """构造 SSE 响应体（每行 data: 前缀，空行分隔）。"""
    body = b""
    for ev in events:
        body += f"data: {ev}\n\n".encode()
    return body


async def test_stream_chat_openai_sse() -> None:
    """OpenAI SSE 流式逐 token 产出（P0-1）。"""
    sse_body = _sse_lines([
        json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
        json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        json.dumps({"choices": [{"delta": {"content": "!"}}]}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        tokens: list[str] = []
        async for token in client.stream_chat([Message(role="user", content="hi")]):
            tokens.append(token)
    finally:
        await client.close()

    assert tokens == ["Hel", "lo", "!"]


async def test_stream_chat_anthropic_sse() -> None:
    """Anthropic SSE 流式（content_block_delta 事件）。"""
    sse_body = _sse_lines([
        json.dumps({"type": "message_start"}),
        json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Bon"}}),
        json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "jour"},
        }),
        json.dumps({"type": "message_stop"}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="anthropic", model="claude", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        tokens: list[str] = []
        async for token in client.stream_chat([Message(role="user", content="hi")]):
            tokens.append(token)
    finally:
        await client.close()

    assert tokens == ["Bon", "jour"]


async def test_stream_chat_unsupported_provider_raises() -> None:
    """不支持的 provider 流式调用抛 LLMError。"""
    config = LLMConfig(provider="openai", model="gpt-4o")
    client = LLMClient(config)
    client.config.provider = "unknown"  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError, match="不支持"):
            async for _ in client.stream_chat([Message(role="user", content="hi")]):
                pass
    finally:
        await client.close()


# ===================== P2-9 TTFT 采集 =====================


async def test_stream_chat_records_ttft_on_first_token() -> None:
    """P2-9：stream_chat 首 token 产出时记录 llm_ttft{model}。"""
    from app.core.metrics import metrics

    metrics.reset()
    sse_body = _sse_lines([
        json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
        json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o-ttft", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        async for _ in client.stream_chat([Message(role="user", content="hi")]):
            pass
    finally:
        await client.close()

    # 首 token 产出后应记录一次 TTFT（非每 token 记录）
    state = metrics.get_histogram("llm_ttft", ("gpt-4o-ttft",))
    assert state.count == 1
    assert state.sum > 0.0
    metrics.reset()


async def test_stream_chat_no_ttft_when_no_tokens() -> None:
    """P2-9：流式无 token 产出时不记录 TTFT。"""
    from app.core.metrics import metrics

    metrics.reset()
    # 空 SSE（仅 [DONE]，无 delta）
    sse_body = _sse_lines(["[DONE]"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o-empty", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        async for _ in client.stream_chat([Message(role="user", content="hi")]):
            pass
    finally:
        await client.close()

    # 无 token → 无 TTFT 记录
    state = metrics.get_histogram("llm_ttft", ("gpt-4o-empty",))
    assert state.count == 0
    metrics.reset()


# ===================== P6e 真 streaming（stream_chat_events） =====================


async def test_stream_chat_events_openai_text_and_finish() -> None:
    """P6e：OpenAI SSE 单流产出 text 事件 + finish 事件含完整 content 与 usage。"""
    sse_body = _sse_lines([
        json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
        json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        json.dumps({"choices": [{"delta": {"content": "!"}}]}),
        json.dumps({"usage": {"total_tokens": 7}, "choices": []}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        events = [
            e
            async for e in client.stream_chat_events(
                [Message(role="user", content="hi")]
            )
        ]
    finally:
        await client.close()

    text_events = [e for e in events if e.type == "text"]
    finish_events = [e for e in events if e.type == "finish"]
    assert [e.content for e in text_events] == ["Hel", "lo", "!"]
    assert len(finish_events) == 1
    # finish.content 为完整拼接
    assert finish_events[0].content == "Hello!"
    # usage 来自末帧
    assert finish_events[0].usage.get("total_tokens") == 7
    # 无工具调用
    assert finish_events[0].tool_calls == []


async def test_stream_chat_events_openai_tool_call_streaming() -> None:
    """P6e：OpenAI 流式 tool_calls 按 index 累积，finish 时给出完整 ToolCall。

    首帧含 id + function.name，后续帧含 function.arguments 片段（JSON 增量）。
    """
    sse_body = _sse_lines([
        # text delta
        json.dumps({"choices": [{"delta": {"content": "Let me calc"}}]}),
        # tool_call 首帧：id + name
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_abc", "function": {"name": "calc", "arguments": ""}}
        ]}}]}),
        # tool_call 后续帧：arguments 片段
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"expr\":"}}
        ]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"1+1\"}"}}
        ]}}]}),
        json.dumps({"usage": {"total_tokens": 12}, "choices": []}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        events = [
            e
            async for e in client.stream_chat_events(
                [Message(role="user", content="算 1+1")],
                tools=[ToolDef(
                    name="calc",
                    description="计算器",
                    parameters={"type": "object", "properties": {}},
                )],
            )
        ]
    finally:
        await client.close()

    text_events = [e for e in events if e.type == "text"]
    tool_events = [e for e in events if e.type == "tool_call"]
    finish_events = [e for e in events if e.type == "finish"]
    assert len(text_events) == 1
    assert text_events[0].content == "Let me calc"
    # 首帧 tool_call 含 id + name
    assert tool_events[0].tool_call_id == "call_abc"
    assert tool_events[0].tool_call_name == "calc"
    # 后续帧 args_delta 累积
    args_delta_concat = "".join(e.args_delta for e in tool_events)
    assert args_delta_concat == '{"expr":"1+1"}'
    # finish 给出完整 ToolCall，args 已解析为 dict
    assert len(finish_events) == 1
    tc = finish_events[0].tool_calls
    assert len(tc) == 1
    assert tc[0].id == "call_abc"
    assert tc[0].name == "calc"
    assert tc[0].args == {"expr": "1+1"}
    assert finish_events[0].usage.get("total_tokens") == 12


async def test_stream_chat_events_openai_multiple_tool_calls_ordered() -> None:
    """P6e：多个 tool_call 按 index 升序组装（顺序与 LLM 输出一致）。"""
    sse_body = _sse_lines([
        # 第一个工具：index 0
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": "{\"q\":"}}
        ]}}]}),
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"x\"}"}}
        ]}}]}),
        # 第二个工具：index 1
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_2", "function": {"name": "calc",
             "arguments": "{\"e\":\"2+2\"}"}}
        ]}}]}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        events = [
            e
            async for e in client.stream_chat_events(
                [Message(role="user", content="hi")]
            )
        ]
    finally:
        await client.close()

    finish = [e for e in events if e.type == "finish"][0]
    assert len(finish.tool_calls) == 2
    # index 升序
    assert finish.tool_calls[0].id == "call_1"
    assert finish.tool_calls[0].name == "search"
    assert finish.tool_calls[0].args == {"q": "x"}
    assert finish.tool_calls[1].id == "call_2"
    assert finish.tool_calls[1].name == "calc"
    assert finish.tool_calls[1].args == {"e": "2+2"}


async def test_stream_chat_events_anthropic_tool_use_streaming() -> None:
    """P6e：Anthropic content_block_start + input_json_delta 解析 tool_use。"""
    sse_body = _sse_lines([
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
        # tool_use block 开始
        json.dumps({
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_xyz", "name": "calc"},
        }),
        # input_json_delta 片段
        json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{\"expr\":"},
        }),
        json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "\"9+1\"}"},
        }),
        # 文本 delta（独立 block index 1）
        json.dumps({
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "done"},
        }),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 8}}),
        json.dumps({"type": "message_stop"}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="anthropic", model="claude", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        events = [
            e
            async for e in client.stream_chat_events(
                [Message(role="user", content="算 9+1")]
            )
        ]
    finally:
        await client.close()

    text_events = [e for e in events if e.type == "text"]
    tool_events = [e for e in events if e.type == "tool_call"]
    finish_events = [e for e in events if e.type == "finish"]
    # text + tool_call 事件
    assert len(text_events) == 1
    assert text_events[0].content == "done"
    # 首帧 tool_call 含 id + name
    assert tool_events[0].tool_call_id == "toolu_xyz"
    assert tool_events[0].tool_call_name == "calc"
    # args_delta 累积
    args_concat = "".join(e.args_delta for e in tool_events)
    assert args_concat == '{"expr":"9+1"}'
    # finish 给出完整 ToolCall
    assert len(finish_events) == 1
    assert finish_events[0].content == "done"
    tc = finish_events[0].tool_calls
    assert len(tc) == 1
    assert tc[0].id == "toolu_xyz"
    assert tc[0].name == "calc"
    assert tc[0].args == {"expr": "9+1"}
    # usage 来自 message_start + message_delta
    assert finish_events[0].usage.get("input_tokens") == 5
    assert finish_events[0].usage.get("output_tokens") == 8


async def test_stream_chat_events_finish_records_llm_call_metric() -> None:
    """P6e：finish 事件到达时记录一次 llm_calls{model}（caller 不 break）。"""
    from app.core.metrics import metrics

    metrics.reset()
    sse_body = _sse_lines([
        json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "[DONE]",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    config = LLMConfig(provider="openai", model="gpt-4o-metric", api_key="k")
    client = _make_client(config, handler, max_retries=0)
    try:
        async for _ in client.stream_chat_events([Message(role="user", content="hi")]):
            pass
    finally:
        await client.close()

    # finish 事件触发一次 llm_calls 计数
    assert metrics.get_counter("llm_calls", ("gpt-4o-metric",)) == 1.0
    metrics.reset()


async def test_stream_chat_events_unsupported_provider_raises() -> None:
    """P6e：stream_chat_events 不支持的 provider 抛 LLMError 并记 llm_errors。"""
    from app.core.metrics import metrics

    metrics.reset()
    config = LLMConfig(provider="openai", model="gpt-4o-bad")
    client = LLMClient(config)
    client.config.provider = "unknown"  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError, match="不支持"):
            async for _ in client.stream_chat_events([Message(role="user", content="hi")]):
                pass
    finally:
        await client.close()
    metrics.reset()


# ===================== _assemble_tool_calls 单测（P6e） =====================


def test_assemble_tool_calls_sorted_by_index() -> None:
    """index 升序组装（即使 acc 字典乱序）。"""
    acc = {
        2: {"id": "c3", "name": "third", "args": "{}"},
        0: {"id": "c1", "name": "first", "args": "{}"},
        1: {"id": "c2", "name": "second", "args": "{}"},
    }
    calls = _assemble_tool_calls(acc)
    assert [c.id for c in calls] == ["c1", "c2", "c3"]
    assert [c.name for c in calls] == ["first", "second", "third"]


def test_assemble_tool_calls_parses_args_json() -> None:
    """args JSON 字符串解析为 dict。"""
    acc = {0: {"id": "x", "name": "calc", "args": '{"expr": "1+1", "n": 2}'}}
    calls = _assemble_tool_calls(acc)
    assert len(calls) == 1
    assert calls[0].args == {"expr": "1+1", "n": 2}


def test_assemble_tool_calls_empty_args_yields_empty_dict() -> None:
    """args 为空字符串时降级为空 dict。"""
    acc = {0: {"id": "x", "name": "noop", "args": ""}}
    calls = _assemble_tool_calls(acc)
    assert len(calls) == 1
    assert calls[0].args == {}


def test_assemble_tool_calls_malformed_args_yields_empty_dict() -> None:
    """args 为非法 JSON 时降级为空 dict，不抛异常。"""
    acc = {0: {"id": "x", "name": "bad", "args": "{not json"}}
    calls = _assemble_tool_calls(acc)
    assert len(calls) == 1
    assert calls[0].args == {}


def test_assemble_tool_calls_empty_acc() -> None:
    """空 acc 返回空列表。"""
    assert _assemble_tool_calls({}) == []


# ===================== C4 prompt cache cached_tokens 指标 =====================


async def test_llm_client_records_openai_cached_tokens() -> None:
    """C4：OpenAI prompt_tokens_details.cached_tokens 被记入 llm_cached_tokens。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 60},
                },
            })

        config = LLMConfig(provider="openai", model="gpt-4o-cache", api_key="k")
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        # cached_tokens 单独记入 llm_cached_tokens{model}
        assert metrics.get_counter(
            "llm_cached_tokens", ("gpt-4o-cache",)
        ) == 60
        # input_tokens 仍是完整 prompt_tokens（不从 input 中扣除 cached）
        assert metrics.get_counter(
            "llm_tokens", ("gpt-4o-cache", "in")
        ) == 100
    finally:
        metrics.reset()


async def test_llm_client_records_anthropic_cached_tokens() -> None:
    """C4：Anthropic cache_read_input_tokens 被记入 llm_cached_tokens。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 120,
                },
            })

        config = LLMConfig(
            provider="anthropic", model="claude-cache", api_key="k"
        )
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        assert metrics.get_counter(
            "llm_cached_tokens", ("claude-cache",)
        ) == 120
        assert metrics.get_counter(
            "llm_tokens", ("claude-cache", "in")
        ) == 200
    finally:
        metrics.reset()


async def test_llm_client_no_cache_field_records_zero_cached_tokens() -> None:
    """C4：usage 无 cache 字段时 llm_cached_tokens 不增长（保持向后兼容）。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })

        config = LLMConfig(provider="openai", model="gpt-4o-nocache", api_key="k")
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        # 无 cache 字段 → counter 未被创建，get_counter 返回默认 0.0
        assert metrics.get_counter(
            "llm_cached_tokens", ("gpt-4o-nocache",)
        ) == 0.0
    finally:
        metrics.reset()


async def test_llm_client_null_prompt_tokens_details_handled() -> None:
    """C4：prompt_tokens_details 为 null 不抛异常，cached_tokens 记 0。"""
    from app.core.metrics import metrics

    metrics.reset()
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_tokens_details": None,
                },
            })

        config = LLMConfig(provider="openai", model="gpt-4o-null", api_key="k")
        client = _make_client(config, handler, max_retries=0)
        try:
            await client.chat([Message(role="user", content="hi")])
        finally:
            await client.close()

        # None 不应抛异常，cached_tokens 记 0
        assert metrics.get_counter(
            "llm_cached_tokens", ("gpt-4o-null",)
        ) == 0.0
    finally:
        metrics.reset()

