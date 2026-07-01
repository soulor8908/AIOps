"""app/core/logging.py 单元测试 — 结构化 JSON 日志。

覆盖（observability.spec.md§2）：
- JsonFormatter 输出 JSON 一行
- 必填字段：timestamp / level / logger / message
- 可选字段：request_id / user_id / latency_ms（ContextVar 注入 + extra 透传）
- 业务额外字段透传（method / path / status / model 等）
- 异常信息附加
- setup_logging 幂等性
- set_request_context / clear_request_context
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    JsonFormatter,
    RequestContextFilter,
    clear_request_context,
    request_id_var,
    set_request_context,
    setup_logging,
)

# ===================== JsonFormatter =====================

def test_json_formatter_required_fields() -> None:
    """必填字段：timestamp / level / logger / message。"""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)

    assert data["level"] == "info"
    assert data["logger"] == "app.test"
    assert data["message"] == "hello world"
    assert "timestamp" in data
    # ISO 8601 UTC 应含时区标识
    assert data["timestamp"].endswith("+00:00")


def test_json_formatter_one_line() -> None:
    """observability.spec.md§2.1：一行一条 JSON。"""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app", level=logging.WARNING, pathname="x", lineno=1,
        msg="multi\nline\nmessage", args=(), exc_info=None,
    )
    output = formatter.format(record)
    # 不应含裸换行（JSON 内部转义为 \n）
    assert output.count("\n") <= 1
    # 能解析为单条 JSON
    json.loads(output)


def test_json_formatter_extra_business_fields() -> None:
    """业务额外字段通过 extra 透传。"""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app.main", level=logging.INFO, pathname="x", lineno=1,
        msg="request completed", args=(), exc_info=None,
    )
    # 模拟 logger.info(..., extra={...})
    record.latency_ms = 12.5
    record.method = "GET"
    record.path = "/health"
    record.status = 200
    output = formatter.format(record)
    data = json.loads(output)

    assert data["latency_ms"] == 12.5
    assert data["method"] == "GET"
    assert data["path"] == "/health"
    assert data["status"] == 200


def test_json_formatter_exception_info() -> None:
    """异常信息附加到 exception 字段。"""
    formatter = JsonFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="app", level=logging.ERROR, pathname="x", lineno=1,
        msg="failed", args=(), exc_info=exc_info,
    )
    output = formatter.format(record)
    data = json.loads(output)

    assert "exception" in data
    assert "ValueError" in data["exception"]
    assert "test error" in data["exception"]


def test_json_formatter_omits_none_optional() -> None:
    """未携带的可选字段不出现。"""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname="x", lineno=1,
        msg="plain", args=(), exc_info=None,
    )
    data = json.loads(formatter.format(record))
    assert "request_id" not in data
    assert "user_id" not in data
    assert "latency_ms" not in data


# ===================== RequestContextFilter =====================

def test_request_context_filter_injects_request_id() -> None:
    """ContextVar 设置后 filter 注入 request_id 到 LogRecord。"""
    set_request_context("req-123")
    try:
        record = logging.LogRecord(
            name="app", level=logging.INFO, pathname="x", lineno=1,
            msg="test", args=(), exc_info=None,
        )
        RequestContextFilter().filter(record)
        assert record.request_id == "req-123"  # type: ignore[attr-defined]
    finally:
        clear_request_context()


def test_request_context_filter_injects_user_id() -> None:
    """ContextVar 设置 user_id 后注入。"""
    set_request_context("req-1", user_id="user-42")
    try:
        record = logging.LogRecord(
            name="app", level=logging.INFO, pathname="x", lineno=1,
            msg="test", args=(), exc_info=None,
        )
        RequestContextFilter().filter(record)
        assert record.user_id == "user-42"  # type: ignore[attr-defined]
    finally:
        clear_request_context()


def test_request_context_filter_no_context() -> None:
    """无上下文时不附加 request_id（observability.spec.md§2.2）。"""
    clear_request_context()
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname="x", lineno=1,
        msg="test", args=(), exc_info=None,
    )
    RequestContextFilter().filter(record)
    assert not hasattr(record, "request_id")


# ===================== set/clear_request_context =====================

def test_set_and_clear_request_context() -> None:
    """set 后 ContextVar 持有值，clear 后归 None。"""
    assert request_id_var.get() is None
    set_request_context("abc-123")
    assert request_id_var.get() == "abc-123"
    clear_request_context()
    assert request_id_var.get() is None


# ===================== setup_logging =====================

def test_setup_logging_idempotent() -> None:
    """setup_logging 重复调用不重复添加 handler。"""
    setup_logging("INFO")
    root = logging.getLogger()
    n1 = len(root.handlers)
    setup_logging("DEBUG")
    n2 = len(root.handlers)
    assert n1 == n2 == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_setup_logging_level_applied() -> None:
    """log_level 正确应用到 root logger。"""
    setup_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING
    setup_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    """实际日志输出为 JSON 格式。"""
    setup_logging("INFO")
    log = logging.getLogger("app.test_emit")
    log.info("hello json", extra={"latency_ms": 5.0})
    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["message"] == "hello json"
    assert data["latency_ms"] == 5.0
    assert data["logger"] == "app.test_emit"
