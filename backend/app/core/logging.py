"""结构化 JSON 日志（observability.spec.md§2）。

设计原则（Karpathy Minimalism）：不引入 ``structlog`` / ``python-json-logger``
等新依赖，仅用标准库 ``logging`` + ``contextvars`` 实现一行一条 JSON 的日志流，
满足 ``observability.spec.md``§2.2 必填字段要求。

必填字段：``timestamp`` / ``level`` / ``logger`` / ``message``
可选字段（按上下文附加）：``request_id`` / ``user_id`` / ``latency_ms``

请求上下文通过 ``ContextVar`` 贯穿同一请求产生的所有日志，无需调用方手动透传。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

# 请求追踪上下文（observability.spec.md§4）。
# ObservabilityMiddleware 在请求入口 set，请求结束 reset。
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


class RequestContextFilter(logging.Filter):
    """注入 ``request_id`` / ``user_id`` 到每条 LogRecord。

    从 ``ContextVar`` 读取，未设置时不附加该字段（observability.spec.md§2.2：
    "无上下文时省略"）。避免日志调用方手动透传 request_id。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        if rid:
            record.request_id = rid
        uid = user_id_var.get()
        if uid is not None:
            record.user_id = uid
        return True


class JsonFormatter(logging.Formatter):
    """一行一条 JSON 日志格式化器（observability.spec.md§2.1）。

    必填字段：``timestamp`` (ISO 8601 UTC) / ``level`` / ``logger`` / ``message``
    可选字段：``request_id`` / ``user_id`` / ``latency_ms``（仅当 LogRecord 携带）
    额外字段：调用方通过 ``logger.info("msg", extra={...})`` 传入的业务字段
    （如 ``method`` / ``path`` / ``status`` / ``model`` / ``prompt_id``）自动透传。

    P0-9：敏感字段脱敏。键名匹配 ``_SENSITIVE_KEY_PREFIXES`` / ``_SENSITIVE_KEYS``
    的 extra 字段值统一替换为 ``"***"``，防止凭据/密钥/PII 落入日志。
    仅按键名匹配——值类型不影响判定（str / dict / list 一视同仁）。
    """

    # 标准 LogRecord 属性集合，不写入 payload（避免噪声）。
    # 调用方通过 ``extra={...}`` 注入的字段不在其中，会被输出。
    _STANDARD_ATTRS: frozenset[str] = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    # P0-9：敏感字段精确匹配集合（小写比较）。
    # 涵盖密码 / token / 密钥 / 连接串 / cookie 等常见凭据字段名。
    _SENSITIVE_KEYS: frozenset[str] = frozenset({
        "password", "passwd", "pwd", "secret", "api_key", "apikey",
        "access_token", "refreshtoken", "refresh_token", "token",
        "authorization", "auth", "bearer", "jwt", "jti",
        "hashed_password", "private_key", "privatekey",
        "client_secret", "clientsecret", "session_id", "sessionid",
        "cookie", "cookies", "set_cookie", "set-cookie",
        "ssn", "credit_card", "creditcard", "card_number", "cardnumber",
        "cvv", "pin",
    })

    # P0-9：敏感字段前缀匹配（小写比较）。命中任一前缀即视为敏感。
    # 覆盖 ``openai_api_key`` / ``anthropic_api_key`` / ``db_password`` 等
    # 复合命名，避免穷举所有变体。
    _SENSITIVE_KEY_PREFIXES: tuple[str, ...] = (
        "password_", "passwd_", "pwd_", "secret_",
        "api_key_", "apikey_", "token_",
        "access_token_", "refresh_token_",
        "private_key_", "client_secret_",
        "authorization_",
    )

    # P0-9：敏感字段子串匹配（小写比较）。命中即视为敏感。
    # 用于 ``x-api-key`` / ``my_token_value`` 等含分隔符的复合键名。
    _SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
        "password", "secret", "api_key", "apikey", "access_token",
        "refresh_token", "private_key", "client_secret",
    )

    @classmethod
    def _is_sensitive_key(cls, key: str) -> bool:
        """判定 extra 字段键名是否敏感（P0-9 脱敏白名单）。

        小写比较；命中精确集合 / 前缀 / 子串任一即返回 True。
        故意宽松——脱敏误伤（把非敏感字段替换为 ``***``）成本远低于
        凭据泄漏成本，宁可多掩码也不漏。
        """
        lk = key.lower()
        if lk in cls._SENSITIVE_KEYS:
            return True
        if any(lk.startswith(p) for p in cls._SENSITIVE_KEY_PREFIXES):
            return True
        if any(s in lk for s in cls._SENSITIVE_KEY_SUBSTRINGS):
            return True
        return False

    @staticmethod
    def _mask_value(value: Any) -> Any:
        """脱敏值。统一替换为 ``"***"``，保留 None 语义（不输出）。"""
        # None 不替换——``format`` 中 ``value is not None`` 已过滤掉 None，
        # 此分支仅作类型完整。保留以便未来扩展（如保留长度提示）。
        if value is None:
            return None
        return "***"

    def format(self, record: logging.LogRecord) -> str:
        # ISO 8601 UTC 时间戳（observability.spec.md§2.2）
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat()

        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 输出所有非标准属性（含 request_id/user_id/latency_ms + 业务 extra 字段）
        for key, value in record.__dict__.items():
            if key in self._STANDARD_ATTRS or key in payload or key.startswith("_"):
                continue
            if value is not None:
                # P0-9：敏感字段键名脱敏（值统一替换为 ***，不输出原始值）
                if self._is_sensitive_key(key):
                    payload[key] = self._mask_value(value)
                else:
                    payload[key] = value

        # 异常信息（ERROR/CRITICAL 必含上下文 observability.spec.md§3）
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_level: str = "INFO") -> None:
    """配置 root logger 使用 JSON 格式化器。

    幂等：重复调用仅重置 handler，不重复添加。
    生产默认 INFO，可通过 ``LOG_LEVEL`` 调整（observability.spec.md§3）。
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    # 清除既有 handler（幂等），避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestContextFilter())
    handler.setLevel(level)
    root.addHandler(handler)

    # uvicorn / sqlalchemy 等第三方 logger 跟随 root 级别
    for noisy in ("uvicorn", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(level)


def set_request_context(
    request_id: str | None, user_id: str | None = None
) -> tuple[Token[str | None], Token[str | None]]:
    """在请求入口设置 request_id / user_id 上下文。

    供 ``ObservabilityMiddleware`` 调用，使同一请求所有日志自动携带。
    返回的 token 必须由 ``reset_request_context`` 消费以正确恢复外层值
    （Python ``contextvars`` 惯用模式，避免协程复用泄漏）。
    """
    rid_token = request_id_var.set(request_id)
    uid_token = user_id_var.set(user_id)
    return rid_token, uid_token


def reset_request_context(
    tokens: tuple[Token[str | None], Token[str | None]]
) -> None:
    """请求结束恢复上下文到 set 前的状态（Python ``contextvars`` 惯用模式）。

    使用 ``reset(token)`` 而非 ``set(None)``，确保嵌套中间件 / 协程复用场景下
    外层 ContextVar 值被正确恢复，避免上一个请求的 request_id 泄漏。
    """
    rid_token, uid_token = tokens
    request_id_var.reset(rid_token)
    user_id_var.reset(uid_token)
