"""自研轻量 LLM 客户端。

零框架依赖，直接封装 OpenAI / Anthropic / local HTTP API。
Understanding-First：所有逻辑可读，无黑盒。

工程要点：
- 统一异常包装：所有 HTTP / JSON / 结构异常都转为 ``LLMError``，调用方无需 catch 多种异常。
- 可重试故障重试：对 429 / 5xx / 网络超时做指数退避重试（默认 2 次），4xx 鉴权/参数错不重试。
- OpenAI 兼容协议合并：``_call_openai`` / ``_call_local`` 共享 ``_call_openai_compatible``。
- 结构化日志：每次调用记录 provider/model/latency_ms/tokens，供排障与可观测性消费。
- 异步上下文管理器：支持 ``async with LLMClient(config) as client:``，杜绝连接泄漏。
- Streaming（P0-1）：``stream_chat`` 异步生成器逐 token 产出，覆盖 OpenAI/Anthropic SSE。
- 真 streaming（P6e）：``stream_chat_events`` 单流承载 text + tool_call 增量，
  调用方实时分流，消除"阻塞 chat + 重跑 stream"的双倍 LLM 成本。
- 原生 function calling（P0-2）：OpenAI tools / Anthropic tool_use，替代文本块解析。
- Structured outputs（P2-10）：``response_format`` json_schema 强约束。
- Prompt caching（P2-10）：``cache_control`` 标记 system/长上下文。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.core.exceptions import LLMError
from app.core.metrics import metrics

logger = logging.getLogger("app.llm_client")

Provider = Literal["openai", "anthropic", "local"]

# 可重试的 HTTP 状态码（瞬时故障）：限流 + 服务端错误。
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = 2
_BASE_BACKOFF_SECONDS = 0.5


async def _iter_lines_with_timeout(
    resp: httpx.Response, timeout: float
) -> AsyncIterator[str]:
    """P0-14：逐行读取 SSE 流，每行带独立超时。

    httpx 的 ``timeout`` 对 streaming 响应的逐次 read 不生效（仅覆盖建连 +
    首字节）。LLM provider 卡死（持续发心跳但不发 data）时 ``aiter_lines()``
    会无限阻塞，占用 httpx 连接 + uvicorn worker。

    本包装对每次 ``aiter_lines()`` 迭代加 ``asyncio.wait_for`` 超时，超时
    抛 ``TimeoutError``——调用方的 ``except`` 块应将其转为 ``_RetryableLLMError``
    进入既有重试逻辑。
    """
    aiter = resp.aiter_lines().__aiter__()
    while True:
        # StopAsyncIteration 透传（正常结束）；TimeoutError 透传（卡死）
        line = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        yield line


@dataclass(slots=True)
class Message:
    """对话消息。role ∈ system/user/assistant/tool。

    cache_control=True 时标记为 prompt cache 锚点（P2-10），
    OpenAI/Anthropic 会对齐 cache 边界省 50%+ 成本。
    """

    role: str
    content: str
    cache_control: bool = False


@dataclass(slots=True)
class ToolDef:
    """原生 function calling 工具定义（OpenAI tools / Anthropic tool_use）。

    parameters 为 JSON Schema dict，由调用方按工具能力构造。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(slots=True)
class ToolCall:
    """LLM 返回的工具调用（结构化，区别于文本块解析）。"""

    id: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


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
    """LLM 响应。tool_calls 为原生 function calling 解析后的结构化调用。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)
    latency_ms: float = 0.0


@dataclass(slots=True)
class StreamEvent:
    """LLM 流式事件（P0-1 真 streaming）。

    type 取值：
    - ``"text"``: ``content`` 为文本 delta（逐 token），调用方应即时渲染。
    - ``"tool_call"``: 工具调用增量。该 tool_call 首帧含 ``tool_call_id`` +
      ``tool_call_name``，后续帧仅含 ``args_delta``（JSON 字符串片段）。
      调用方按 ``tool_call_id`` 累积，``finish`` 事件给出完整 ``tool_calls``。
    - ``"finish"``: 流结束。``content`` 为完整文本，``tool_calls`` 为完整调用
      列表（已解析 args），``usage`` 为 token 统计。

    设计：单流承载 text + tool_call，调用方在流里实时分流，避免"阻塞 chat 判断
    工具 + 重跑 stream 输出文本"的双倍 LLM 成本。
    """

    type: str
    content: str = ""
    tool_call_id: str = ""
    tool_call_name: str = ""
    args_delta: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


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
        self._timeout = timeout
        # 懒初始化：避免构造期即创建 httpx.AsyncClient 连接池。
        # 调用方常以 ``LLMClient(config)`` 构造，若随后因异常未进入
        # ``try/finally close``（如 ``_build_llm_config`` 抛错），急切创建的
        # 连接池会泄漏。懒初始化确保仅在真正发请求时才占用连接资源。
        self._http: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        """懒初始化 httpx.AsyncClient（首次访问或上次已关闭时创建）。"""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ===================== 阻塞调用（向后兼容） =====================

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """按 provider 分发调用，带重试。

        所有异常（HTTP / JSON 解析 / 响应结构异常）统一包装为 ``LLMError``。
        仅对可重试故障（429/5xx/网络超时）重试，4xx 鉴权/参数错立即抛出。

        - ``tools``：原生 function calling（P0-2），替代文本块解析。
        - ``response_format``：json_schema 强约束结构化输出（P2-10）。
        """
        dispatch = {
            "openai": self._call_openai,
            "anthropic": self._call_anthropic,
            "local": self._call_local,
        }
        handler = dispatch.get(self.config.provider)
        if handler is None:
            metrics.record_llm_error(self.config.model, "unsupported_provider")
            raise LLMError(f"不支持的 provider: {self.config.provider}")

        last_exc: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                response = await handler(messages, tools, response_format)
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
                # 重试耗尽：记录失败指标后跳出
                metrics.record_llm_error(self.config.model, "retryable_exhausted")
                break
            except LLMError:
                # 不可重试的 LLMError：记录失败指标后立即抛出（不重试 4xx 鉴权/参数错）。
                metrics.record_llm_error(self.config.model, "non_retryable")
                raise
            latency_ms = (time.monotonic() - start) * 1000
            response.latency_ms = latency_ms
            self._record_usage(response)
            # P2-9：成功调用计数，配合 llm_errors 估算错误率（AI 系统健康度）。
            metrics.record_llm_call(self.config.model)
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

    # ===================== Streaming（P0-1 / P6e 真 streaming） =====================

    async def stream_chat_events(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """真 streaming（P6e）：单流 yield 结构化事件，承载 text + tool_call 增量。

        相比旧 ``stream_chat``（仅 text）+ executor 双调 LLM 的方案，本方法在
        单条流里实时分流 text delta 与 tool_call delta，调用方一次消费即可同时
        渲染打字机效果并解析工具调用，**消除"阻塞 chat 判断工具 + 重跑 stream
        输出文本"的双倍 LLM 成本**。

        yield 事件（``StreamEvent``）：
        - ``type="text"``: ``content`` 为文本 delta
        - ``type="tool_call"``: 工具调用增量（首帧含 id+name，后续帧含 args_delta）
        - ``type="finish"``: 流结束，含完整 ``content`` / ``tool_calls`` / ``usage``

        TTFT 在首个 text/tool_call 事件时记录；finish 事件到达即记 ``llm_call``
        （在 yield 前记录，避免 caller break 后漏记），失败记 ``llm_error``。
        ``stream_chat``（文本-only）为本方法的薄包装，向后兼容。
        """
        dispatch = {
            "openai": self._stream_openai_events,
            "local": self._stream_openai_events,
            "anthropic": self._stream_anthropic_events,
        }
        streamer = dispatch.get(self.config.provider)
        if streamer is None:
            metrics.record_llm_error(self.config.model, "unsupported_provider")
            raise LLMError(f"不支持的 provider: {self.config.provider}")

        start = time.monotonic()
        first_event_recorded = False
        try:
            async for event in streamer(messages, tools):
                # P2-9：首个 text/tool_call 事件产出时记录 TTFT（finish 不算首 token）
                if not first_event_recorded and event.type in ("text", "tool_call"):
                    ttft_ms = (time.monotonic() - start) * 1000
                    metrics.record_ttft(self.config.model, ttft_ms)
                    first_event_recorded = True
                # P6e：finish 事件到达即记 llm_call（yield 前记录，caller break 不漏记）
                if event.type == "finish":
                    metrics.record_llm_call(self.config.model)
                yield event
        except _RetryableLLMError as exc:
            metrics.record_llm_error(self.config.model, "retryable_exhausted")
            raise LLMError(f"流式调用可重试故障耗尽: {exc}") from exc
        except LLMError:
            metrics.record_llm_error(self.config.model, "non_retryable")
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            logger.info(
                "llm_stream provider=%s model=%s latency_ms=%.0f",
                self.config.provider,
                self.config.model,
                latency_ms,
            )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncIterator[str]:
        """流式逐 token 产出文本（向后兼容，P0-1）。

        本方法为 ``stream_chat_events`` 的薄包装：仅 yield text 事件的 content，
        丢弃 tool_call / finish 事件。需要工具调用流式解析的应用应直接使用
        ``stream_chat_events``。
        """
        async for event in self.stream_chat_events(messages, tools):
            if event.type == "text":
                yield event.content

    async def _stream_openai_events(
        self, messages: list[Message], tools: list[ToolDef] | None
    ) -> AsyncIterator[StreamEvent]:
        """OpenAI 兼容协议 SSE 流，yield 结构化事件（text / tool_call / finish）。

        OpenAI 流式 tool_calls：``delta.tool_calls`` 按 ``index`` 标识调用顺序，
        首帧含 ``id`` + ``function.name``，后续帧仅含 ``function.arguments``
        片段（JSON 字符串增量）。本方法按 index 累积，finish 时组装完整 ToolCall。
        """
        url = self._openai_base_url()
        payload = self._openai_payload(messages, tools, stream=True)
        headers = self._openai_headers()
        # 按 index 累积 tool_call：{index: {"id":..., "name":..., "args": str}}
        tool_acc: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        usage: dict[str, int] = {}
        try:
            async with self.http.stream(
                "POST", f"{url}/chat/completions", headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    if resp.status_code in _RETRYABLE_STATUS_CODES:
                        raise _RetryableLLMError(
                            f"LLM 流式返回可重试状态 {resp.status_code}: {body[:200]!r}"
                        )
                    raise LLMError(f"LLM 流式 HTTP {resp.status_code}: {body[:200]!r}")
                # P0-14：逐 chunk 超时——provider 卡死（心跳不发 data）时
                # ``aiter_lines`` 会无限阻塞，此处每行加超时强制进入重试。
                from app.core.config import settings as _settings

                async for line in _iter_lines_with_timeout(
                    resp, _settings.llm_stream_chunk_timeout_seconds
                ):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    # text delta
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        yield StreamEvent(type="text", content=delta["content"])
                    # tool_call delta（按 index 累积）
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        func = tc_delta.get("function") or {}
                        slot = tool_acc.setdefault(
                            idx, {"id": "", "name": "", "args": ""}
                        )
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        if func.get("name"):
                            slot["name"] = func["name"]
                        args_chunk = func.get("arguments") or ""
                        if args_chunk:
                            slot["args"] += args_chunk
                            yield StreamEvent(
                                type="tool_call",
                                tool_call_id=slot["id"],
                                tool_call_name=slot["name"],
                                args_delta=args_chunk,
                            )
        except httpx.HTTPError as exc:
            raise _RetryableLLMError(f"LLM 流式网络错误: {exc}") from exc
        except TimeoutError as exc:
            # P0-14：逐 chunk 超时——provider 卡死，转为可重试错误进入重试循环
            raise _RetryableLLMError(f"LLM 流式 chunk 超时: {exc}") from exc
        yield StreamEvent(
            type="finish",
            content="".join(text_parts),
            tool_calls=_assemble_tool_calls(tool_acc),
            usage=usage,
        )

    async def _stream_anthropic_events(
        self, messages: list[Message], tools: list[ToolDef] | None
    ) -> AsyncIterator[StreamEvent]:
        """Anthropic SSE 流，yield 结构化事件。

        Anthropic 流式 tool_use：
        - ``content_block_start`` (type=tool_use) 给出 id + name
        - ``content_block_delta`` (type=input_json_delta) 给出 partial_json 片段
        - ``content_block_delta`` (type=text_delta) 给出文本
        - ``message_start`` / ``message_delta`` 给出 input/output tokens
        """
        url = self.config.base_url or "https://api.anthropic.com/v1"
        payload = self._anthropic_payload(messages, tools, stream=True)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }
        # 按 block index 累积 tool_use：{index: {"id":..., "name":..., "args": str}}
        tool_acc: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        usage: dict[str, int] = {}
        try:
            async with self.http.stream(
                "POST", f"{url}/messages", headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    if resp.status_code in _RETRYABLE_STATUS_CODES:
                        raise _RetryableLLMError(
                            f"Anthropic 流式可重试状态 {resp.status_code}: {body[:200]!r}"
                        )
                    raise LLMError(
                        f"Anthropic 流式 HTTP {resp.status_code}: {body[:200]!r}"
                    )
                # P0-14：逐 chunk 超时（同 OpenAI 路径）
                from app.core.config import settings as _settings

                async for line in _iter_lines_with_timeout(
                    resp, _settings.llm_stream_chunk_timeout_seconds
                ):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            idx = event.get("index", 0)
                            slot = tool_acc.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            slot["id"] = block.get("id", "")
                            slot["name"] = block.get("name", "")
                            yield StreamEvent(
                                type="tool_call",
                                tool_call_id=slot["id"],
                                tool_call_name=slot["name"],
                            )
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            txt = delta.get("text") or ""
                            if txt:
                                text_parts.append(txt)
                                yield StreamEvent(type="text", content=txt)
                        elif dtype == "input_json_delta":
                            idx = event.get("index", 0)
                            slot = tool_acc.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            chunk = delta.get("partial_json") or ""
                            slot["args"] += chunk
                            yield StreamEvent(
                                type="tool_call",
                                tool_call_id=slot["id"],
                                args_delta=chunk,
                            )
                    elif etype == "message_start":
                        u = (event.get("message") or {}).get("usage") or {}
                        if u.get("input_tokens"):
                            usage["input_tokens"] = u["input_tokens"]
                    elif etype == "message_delta":
                        u = event.get("usage") or {}
                        if u.get("output_tokens"):
                            usage["output_tokens"] = u["output_tokens"]
        except httpx.HTTPError as exc:
            raise _RetryableLLMError(f"Anthropic 流式网络错误: {exc}") from exc
        except TimeoutError as exc:
            # P0-14：逐 chunk 超时——provider 卡死，转为可重试错误进入重试循环
            raise _RetryableLLMError(f"Anthropic 流式 chunk 超时: {exc}") from exc
        yield StreamEvent(
            type="finish",
            content="".join(text_parts),
            tool_calls=_assemble_tool_calls(tool_acc),
            usage=usage,
        )

    # ===================== OpenAI 兼容协议 =====================

    def _openai_base_url(self) -> str:
        if self.config.provider == "local":
            return self.config.base_url or "http://localhost:11434/v1"
        return self.config.base_url or "https://api.openai.com/v1"

    def _openai_headers(self) -> dict[str, str]:
        if self.config.provider == "local":
            return {}
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def _openai_payload(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        # 原生 function calling（P0-2）：tools 走结构化 API 而非文本块
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        # Structured outputs（P2-10）：json_schema 强约束
        if response_format is not None:
            payload["response_format"] = response_format
        return payload

    async def _call_openai_compatible(
        self,
        base_url: str,
        messages: list[Message],
        headers: dict[str, str],
        tools: list[ToolDef] | None,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        """OpenAI 兼容协议共享实现（OpenAI / local / vLLM / Ollama 等）。

        合并原 ``_call_openai`` 与 ``_call_local`` 的重复逻辑，差异仅由
        ``base_url`` 与 ``headers`` 注入。支持原生 function calling。
        """
        payload = self._openai_payload(messages, tools, response_format=response_format)
        try:
            resp = await self.http.post(
                f"{base_url}/chat/completions", headers=headers, json=payload
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
        tool_calls = _parse_openai_tool_calls(choice.get("tool_calls", []))
        return LLMResponse(
            content=choice.get("content", "") or "",
            tool_calls=tool_calls,
            usage=data.get("usage", {}),
            raw=data,
        )

    async def _call_openai(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        """OpenAI Chat Completions（携带 Authorization + 原生 tools）。"""
        return await self._call_openai_compatible(
            self._openai_base_url(),
            messages,
            self._openai_headers(),
            tools,
            response_format,
        )

    async def _call_local(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        """本地推理服务（OpenAI 兼容协议，无鉴权）。"""
        return await self._call_openai_compatible(
            self._openai_base_url(), messages, self._openai_headers(), tools, response_format
        )

    # ===================== Anthropic =====================

    def _anthropic_payload(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        system_msgs = []
        # cache_control 标记：Anthropic prompt caching（P2-10），省 50%+ 成本
        for m in messages:
            if m.role == "system":
                # 显式 Any：cache_control 值为 dict，与 text 字符串混存需放宽值类型。
                block: dict[str, Any] = {"type": "text", "text": m.content}
                if m.cache_control:
                    block["cache_control"] = {"type": "ephemeral"}
                system_msgs.append(block)
        chat_msgs = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": self.config.model,
            "system": system_msgs or None,
            "messages": chat_msgs,
            "max_tokens": self.config.max_tokens,
            # Anthropic temperature 范围 0-1，clamp 防越界（LLMConfig 允许 0-2）
            "temperature": min(self.config.temperature, 1.0),
            "stream": stream,
        }
        # 原生 tool_use（P0-2）：Anthropic tools schema
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        # Anthropic 不支持 response_format json_schema，但支持 tool 强制结构化。
        # 调用方对 Anthropic 应传 tools 而非 response_format。
        return payload

    async def _call_anthropic(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None,
        response_format: dict[str, Any] | None,
    ) -> LLMResponse:
        """Anthropic Messages API。system 抽离至顶层，支持 tool_use。"""
        url = self.config.base_url or "https://api.anthropic.com/v1"
        payload = self._anthropic_payload(messages, tools)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            resp = await self.http.post(f"{url}/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            args=block.get("input", {}) or {},
                        )
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
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=data.get("usage", {}),
            raw=data,
        )

    # ===================== 指标采集 =====================

    def _record_usage(self, response: LLMResponse) -> None:
        """记录 token 与成本指标（observability.spec.md§5.1）。

        C4：额外提取 prompt cache 命中的 token 数（``cached_tokens``）记入
        ``llm_cached_tokens{model}``，用于量化 prompt caching 实际节省。
        - OpenAI: ``usage.prompt_tokens_details.cached_tokens``
        - Anthropic: ``usage.cache_read_input_tokens``
        两者均缺省时记 0（保持向后兼容，老 provider 无 cache 字段不受影响）。
        """
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
        # C4：prompt cache 命中 token 数（OpenAI / Anthropic 字段名不同，统一提取）
        details = response.usage.get("prompt_tokens_details") or {}
        cached_tokens = int(
            details.get("cached_tokens")
            or response.usage.get("cache_read_input_tokens")
            or 0
        )
        metrics.record_llm_usage(
            model=self.config.model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost=cost,
            cached_tokens=cached_tokens,
        )

    async def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()


# ===================== 应用级连接池单例（P1-5） =====================

# 模块级单例缓存，按 (provider, base_url, api_key, model) hash 缓存 LLMClient 实例。
# 适用于应用级长期复用场景（如 evals judge client），避免每次调用 new 一个
# httpx.AsyncClient 连接池。``agents/service.py`` 的请求级独立 client 模式
# 不走此缓存（资源隔离优先），关闭由各自 try/finally 负责。
_clients: dict[tuple[str, str, str, str], LLMClient] = {}


def get_llm_client(config: LLMConfig) -> LLMClient:
    """获取复用的 LLMClient（按 config 关键字段 hash 单例化）。

    相同 provider+base_url+api_key+model 的配置复用同一个 LLMClient，
    避免每次调用 new 一个 httpx.AsyncClient 连接池。应用关闭时由
    ``close_all_clients()`` 统一释放——调用方**不应**自行 close 单例 client，
    否则会破坏后续复用。

    适用场景：应用级长期复用（evals judge、后台批处理）。
    不适用：``agents`` 请求级独立 client（执行期可能很长，独立 client 便于
    资源隔离与显式释放，保持现有 ``try/finally close`` 模式）。
    """
    key = (config.provider, config.base_url, config.api_key, config.model)
    if key not in _clients:
        _clients[key] = LLMClient(config)
    return _clients[key]


async def close_all_clients() -> None:
    """应用关闭时释放所有缓存的 LLMClient（lifespan shutdown 调用）。"""
    for client in _clients.values():
        await client.close()
    _clients.clear()


class _RetryableLLMError(LLMError):
    """可重试的 LLM 错误（429/5xx/网络超时），供 ``chat`` 重试循环识别。"""


class ToolCallParser(Protocol):
    """工具调用解析协议（供 executor 复用）。"""

    def __call__(self, content: str) -> list[dict[str, object]]: ...


# ===================== streaming tool_call 组装 =====================


def _assemble_tool_calls(acc: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """把按 index 累积的 tool_call 字典组装为 ToolCall 列表（P6e 真 streaming）。

    ``acc`` 形如 ``{0: {"id": "call_x", "name": "calc", "args": '{"expr":"1+1"}'}}``，
    ``args`` 为流式累积的 JSON 字符串，此处二次解析为 dict。index 升序保证
    多工具调用顺序与 LLM 输出一致。args 解析失败时降级为空 dict（不阻塞流）。
    """
    calls: list[ToolCall] = []
    for idx in sorted(acc):
        slot = acc[idx]
        args_raw = slot.get("args", "")
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            args = {}
        calls.append(
            ToolCall(
                id=slot.get("id", ""),
                name=slot.get("name", ""),
                args=args,
            )
        )
    return calls


# ===================== 原生 tool call 解析 =====================


def _parse_openai_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall]:
    """解析 OpenAI tool_calls 结构为 ToolCall 列表（P0-2）。

    OpenAI 格式：``[{"id":"...","function":{"name":"...","arguments":"{...}"}}]``
    arguments 是 JSON 字符串需二次解析。
    """
    if not raw:
        return []
    calls: list[ToolCall] = []
    for item in raw:
        func = item.get("function") or {}
        args_raw = func.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        calls.append(
            ToolCall(id=item.get("id", ""), name=func.get("name", ""), args=args)
        )
    return calls


# ===================== 向后兼容：文本块 tool call 解析 =====================


def parse_tool_calls_json(content: str) -> list[dict[str, object]]:
    """从 LLM 输出中解析 ```tool_calls ...``` 块（向后兼容，不推荐新代码使用）。

    P0-2 起推荐原生 function calling（LLMClient.chat(tools=...)），
    新代码应使用 ToolCall 结构化返回。
    """
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


__all__ = [
    "LLMClient",
    "LLMConfig",
    "LLMResponse",
    "Message",
    "StreamEvent",
    "ToolCall",
    "ToolCallParser",
    "ToolDef",
    "close_all_clients",
    "get_llm_client",
    "parse_tool_calls_json",
]
