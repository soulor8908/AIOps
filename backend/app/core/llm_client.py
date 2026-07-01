"""自研轻量 LLM 客户端。

零框架依赖，直接封装 OpenAI / Anthropic / local HTTP API。
Understanding-First：所有逻辑可读，无黑盒。

工程要点：
- 统一异常包装：所有 HTTP / JSON / 结构异常都转为 ``LLMError``，调用方无需 catch 多种异常。
- 可重试故障重试：对 429 / 5xx / 网络超时做指数退避重试（默认 2 次），4xx 鉴权/参数错不重试。
- OpenAI 兼容协议合并：``_call_openai`` / ``_call_local`` 共享 ``_call_openai_compatible``。
- 结构化日志：每次调用记录 provider/model/latency_ms/tokens，供排障与可观测性消费。
- 异步上下文管理器：支持 ``async with LLMClient(config) as client:``，杜绝连接泄漏。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from app.core.exceptions import LLMError
from app.core.metrics import metrics

logger = logging.getLogger("app.llm_client")

Provider = Literal["openai", "anthropic", "local"]

# 可重试的 HTTP 状态码（瞬时故障）：限流 + 服务端错误。
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = 2
_BASE_BACKOFF_SECONDS = 0.5


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
    # 单价（每 1k tokens，美元）。observability.spec.md§5.1 llm_cost 计算。
    # 由 agents service._build_llm_config 从 model_configs 表透传。
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0

    def __post_init__(self) -> None:
        """校验 temperature 在 [0, 2] 范围内。"""
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(
                f"temperature 必须在 0-2 之间，当前: {self.temperature}"
            )
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens 必须 > 0，当前: {self.max_tokens}")


@dataclass(slots=True)
class LLMResponse:
    """LLM 响应。tool_calls 为解析后的工具调用列表。"""

    content: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)
    latency_ms: float = 0.0


class LLMClient:
    """统一 LLM 客户端。chat 方法按 provider 分发。

    支持 ``async with LLMClient(config) as client:`` 用法，确保 httpx 连接释放。
    """

    def __init__(
        self,
        config: LLMConfig,
        timeout: float = 60.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self.config = config
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def chat(self, messages: list[Message]) -> LLMResponse:
        """按 provider 分发调用，带重试。

        所有异常（HTTP / JSON 解析 / 响应结构异常）统一包装为 ``LLMError``。
        仅对可重试故障（429/5xx/网络超时）重试，4xx 鉴权/参数错立即抛出。
        """
        dispatch = {
            "openai": self._call_openai,
            "anthropic": self._call_anthropic,
            "local": self._call_local,
        }
        handler = dispatch.get(self.config.provider)
        if handler is None:
            raise LLMError(f"不支持的 provider: {self.config.provider}")

        last_exc: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                response = await handler(messages)
            except _RetryableLLMError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    backoff = _BASE_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(
                        "llm_call retryable failure provider=%s model=%s attempt=%d/%d "
                        "will_retry_in=%.1fs error=%s",
                        self.config.provider,
                        self.config.model,
                        attempt + 1,
                        self.max_retries + 1,
                        backoff,
                        exc,
                    )
                    await asyncio.sleep(backoff)
                    continue
                break
            except LLMError:
                # 不可重试的 LLMError，立即抛出（不重试 4xx 鉴权/参数错）。
                raise
            latency_ms = (time.monotonic() - start) * 1000
            response.latency_ms = latency_ms
            # 指标采集（observability.spec.md§5.1：llm_tokens / llm_cost）。
            # OpenAI/Anthropic usage 字段名差异：prompt_tokens / input_tokens 同义。
            in_tokens = int(
                response.usage.get("prompt_tokens")
                or response.usage.get("input_tokens")
                or 0
            )
            out_tokens = int(
                response.usage.get("completion_tokens")
                or response.usage.get("output_tokens")
                or 0
            )
            cost = (
                in_tokens / 1000.0 * self.config.cost_per_1k_input
                + out_tokens / 1000.0 * self.config.cost_per_1k_output
            )
            metrics.record_llm_usage(
                model=self.config.model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost=cost,
            )
            logger.info(
                "llm_call provider=%s model=%s latency_ms=%.0f tokens=%s",
                self.config.provider,
                self.config.model,
                latency_ms,
                response.usage.get("total_tokens", 0),
            )
            return response

        # 重试耗尽，抛出最后一次可重试异常
        assert last_exc is not None
        raise last_exc

    async def _call_openai(self, messages: list[Message]) -> LLMResponse:
        """OpenAI Chat Completions（携带 Authorization + usage 解析）。"""
        url = self.config.base_url or "https://api.openai.com/v1"
        return await self._call_openai_compatible(
            url, messages, headers={"Authorization": f"Bearer {self.config.api_key}"}
        )

    async def _call_local(self, messages: list[Message]) -> LLMResponse:
        """本地推理服务（OpenAI 兼容协议，无鉴权）。"""
        url = self.config.base_url or "http://localhost:11434/v1"
        return await self._call_openai_compatible(url, messages, headers={})

    async def _call_openai_compatible(
        self,
        base_url: str,
        messages: list[Message],
        headers: dict[str, str],
    ) -> LLMResponse:
        """OpenAI 兼容协议共享实现（OpenAI / local / vLLM / Ollama 等）。

        合并原 ``_call_openai`` 与 ``_call_local`` 的重复逻辑，差异仅由
        ``base_url`` 与 ``headers`` 注入。
        """
        try:
            resp = await self._http.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.config.model,
                    "messages": [
                        {"role": m.role, "content": m.content} for m in messages
                    ],
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUS_CODES:
                raise _RetryableLLMError(
                    f"LLM 返回可重试状态 {status}: {exc.response.text[:200]}"
                ) from exc
            raise LLMError(f"LLM HTTP {status}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            # 网络超时/连接错误 → 可重试
            raise _RetryableLLMError(f"LLM 网络错误: {exc}") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            # 响应 200 但结构异常 → 不可重试（provider 返回了非预期格式）
            raise LLMError(f"LLM 响应结构异常: {exc}") from exc
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
        try:
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
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUS_CODES:
                raise _RetryableLLMError(
                    f"Anthropic 返回可重试状态 {status}: {exc.response.text[:200]}"
                ) from exc
            raise LLMError(f"Anthropic HTTP {status}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise _RetryableLLMError(f"Anthropic 网络错误: {exc}") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Anthropic 响应结构异常: {exc}") from exc
        return LLMResponse(content=text, usage=data.get("usage", {}), raw=data)

    async def close(self) -> None:
        await self._http.aclose()


class _RetryableLLMError(LLMError):
    """可重试的 LLM 错误（429/5xx/网络超时），供 ``chat`` 重试循环识别。"""


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
