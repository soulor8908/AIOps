"""自研轻量 LLM 客户端（~80 行）。

零框架依赖，直接封装 OpenAI / Anthropic / local HTTP API。
Understanding-First：所有逻辑可读，无黑盒。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from app.core.exceptions import LLMError

Provider = Literal["openai", "anthropic", "local"]


@dataclass(slots=True)
class Message:
    """对话消息。role ∈ system/user/assistant/tool。"""

    role: str
    content: str


@dataclass(slots=True)
class LLMConfig:
    """LLM 调用配置。"""

    provider: Provider
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        """校验 temperature 在 [0, 2] 范围内。"""
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(
                f"temperature 必须在 0-2 之间，当前: {self.temperature}"
            )


@dataclass(slots=True)
class LLMResponse:
    """LLM 响应。tool_calls 为解析后的工具调用列表。"""

    content: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)
    latency_ms: float = 0.0


class LLMClient:
    """统一 LLM 客户端。chat 方法按 provider 分发。"""

    def __init__(self, config: LLMConfig, timeout: float = 60.0) -> None:
        self.config = config
        self._http = httpx.AsyncClient(timeout=timeout)

    async def chat(self, messages: list[Message]) -> LLMResponse:
        """按 provider 分发调用。"""
        dispatch = {
            "openai": self._call_openai,
            "anthropic": self._call_anthropic,
            "local": self._call_local,
        }
        handler = dispatch.get(self.config.provider)
        if handler is None:
            raise LLMError(f"不支持的 provider: {self.config.provider}")
        start = time.monotonic()
        try:
            response = await handler(messages)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM HTTP 调用失败: {exc}") from exc
        response.latency_ms = (time.monotonic() - start) * 1000
        return response

    async def _call_openai(self, messages: list[Message]) -> LLMResponse:
        """OpenAI Chat Completions 完整实现。"""
        url = self.config.base_url or "https://api.openai.com/v1"
        resp = await self._http.post(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "model": self.config.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]
        return LLMResponse(
            content=choice.get("content", "") or "",
            tool_calls=choice.get("tool_calls", []) or [],
            usage=data.get("usage", {}),
            raw=data,
        )

    async def _call_anthropic(self, messages: list[Message]) -> LLMResponse:
        """Anthropic Messages API。system 抽离至顶层。"""
        url = self.config.base_url or "https://api.anthropic.com/v1"
        system_msgs = [m.content for m in messages if m.role == "system"]
        chat_msgs = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role != "system"
        ]
        resp = await self._http.post(
            f"{url}/messages",
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": self.config.model,
                "system": "\n\n".join(system_msgs),
                "messages": chat_msgs,
                "max_tokens": self.config.max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        return LLMResponse(content=text, usage=data.get("usage", {}), raw=data)

    async def _call_local(self, messages: list[Message]) -> LLMResponse:
        """本地推理服务（OpenAI 兼容协议）。"""
        url = self.config.base_url or "http://localhost:11434/v1"
        resp = await self._http.post(
            f"{url}/chat/completions",
            json={
                "model": self.config.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]["message"]
        return LLMResponse(content=choice.get("content", "") or "", raw=data)

    async def close(self) -> None:
        await self._http.aclose()


class ToolCallParser(Protocol):
    """工具调用解析协议（供 executor 复用）。"""

    def __call__(self, content: str) -> list[dict[str, object]]: ...


def parse_tool_calls_json(content: str) -> list[dict[str, object]]:
    """从 LLM 输出中解析 ```tool_calls ...``` 块。"""
    marker = "```tool_calls"
    if marker not in content:
        return []
    start = content.index(marker) + len(marker)
    end = content.index("```", start) if "```" in content[start:] else len(content)
    block = content[start:end].strip()
    try:
        parsed = json.loads(block)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return []
