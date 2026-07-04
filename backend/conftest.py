"""Pytest 全局配置：为 SQLite 测试环境注册 PG 专属类型与函数。

生产使用 PostgreSQL（init.sql 定义 JSONB / VECTOR(1536) / date_trunc），
单元测试用 SQLite in-memory 以避免 PG 依赖。此处仅在 SQLite 方言上：

1. 把 JSONB / VECTOR 列 DDL 渲染为 JSON（保持生产 ORM 类型定义忠实于 SPEC）。
2. 把 TSVECTOR 列 DDL 渲染为 TEXT（仅作存储，全文检索在 service 层降级为 LIKE）。
3. 注册 ``date_trunc(unit, ts)`` Python UDF，供 analytics dashboard 按天聚合。

不改动任何生产模型或 service。``tests/conftest.py`` 负责 TestClient 级别的
DB 隔离（StaticPool + monkeypatch init_db / engine）。
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import TSVECTOR as TSVector
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles

# ---------- PG 类型在 SQLite 上的 DDL 渲染 ----------

@compiles(JSONB, "sqlite")
def _jsonb_to_json_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    """SQLite 上将 JSONB 列渲染为 JSON。"""
    return "JSON"


@compiles(Vector, "sqlite")
def _vector_to_json_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    """SQLite 上将 VECTOR 列渲染为 JSON（测试不写入真实向量）。"""
    return "JSON"


@compiles(TSVector, "sqlite")
def _tsvector_to_text_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
    """SQLite 上将 TSVECTOR 列渲染为 TEXT（仅作存储，BM25 检索降级为 LIKE）。

    生产 PG 上为真正的 tsvector 类型并由 GIN 索引加速；SQLite 无 tsvector，
    service 层按方言判断：PG 走 ``ts_rank_cd`` + ``@@``，SQLite 走 ``content LIKE``。
    """
    return "TEXT"


# CITEXT 在 SQLite 上渲染为 TEXT（大小写不敏感由应用层 email.lower() 归一化保证）
try:
    from sqlalchemy.dialects.postgresql import CITEXT

    @compiles(CITEXT, "sqlite")
    def _citext_sqlite(element: Any, compiler: Any, **kw: Any) -> str:
        return "TEXT"
except ImportError:
    pass


# ---------- date_trunc UDF（analytics dashboard 按天聚合） ----------

def _sqlite_date_trunc(unit: str, value: Any) -> str:
    """SQLite 版 date_trunc：仅支持 day 粒度，截断到当天零点。

    value 可能是 'YYYY-MM-DD HH:MM:SS.ffffff' 字符串或 datetime。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        fmts = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
        dt = None
        for fmt in fmts:
            try:
                dt = datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return text[:10]
    if unit == "day":
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


@event.listens_for(Engine, "connect")
def _register_date_trunc(dbapi_connection: Any, connection_record: Any) -> None:
    """在每个 SQLite 连接上注册 date_trunc 函数（其他方言无副作用）。

    aiosqlite.Connection 把真正的 sqlite3.Connection 包在 ``_conn`` 里，
    故需先解包再调用 ``create_function``。
    """
    raw = getattr(dbapi_connection, "_conn", dbapi_connection)
    create_fn = getattr(raw, "create_function", None)
    if create_fn is not None and callable(create_fn):
        with contextlib.suppress(Exception):
            create_fn("date_trunc", 2, _sqlite_date_trunc)
