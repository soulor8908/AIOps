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
    parse_tool_calls_json,
)

# ===================== 辅助函数 =====================

def _make_client(config: LLMConfig, handler: Any) -> LLMClient:
    """构造使用 MockTransport 的 LLMClient。"""
    transport = httpx.MockTransport(handler)
    client = LLMClient(config)
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
    assert d == {"role": "user", "content": "hello world"}

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
    """OpenAI 响应包含 tool_calls 时正确解析。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{"name": "search", "args": {"q": "test"}}],
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
    assert response.tool_calls[0]["name"] == "search"


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

    # system 消息应被抽离到顶层
    assert captured["body"]["system"] == "You are a translator."
    # messages 中不含 system
    assert all(m["role"] != "system" for m in captured["body"]["messages"])
    assert len(captured["body"]["messages"]) == 1

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

    system_field = captured["body"]["system"]
    assert "Rule 1." in system_field
    assert "Rule 2." in system_field
    assert "\n\n" in system_field  # 合并分隔符


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
