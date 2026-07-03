# 依赖声明与理由

> 每个依赖必须通过 50 行测试门槛方可引入。遵循 Minimalism 原则，能 50 行内实现的不引入库。

## 运行时依赖

| 依赖 | 版本 | 引入理由 | 替代方案评估 |
|------|------|---------|--------------|
| `fastapi` | >=0.115 | API 框架，原生 Pydantic v2、自动 OpenAPI、依赖注入、async 路由。AIOps 控制台的核心契约层。 | Flask（无原生 async）、Litestar（生态弱）。FastAPI 是 AI 时代 Python 后端事实标准。 |
| `uvicorn[standard]` | >=0.32 | ASGI 服务器，支持 HTTP/1.1 与 HTTP/2，standard 包含 uvloop + httptools 高性能组件。 | hypercorn（功能多但慢）、gunicorn（同步模型，不适合 async LLM 流式调用）。 |
| `pydantic` | >=2.9 | 数据校验与序列化，Rust 核心性能 10x，类型即文档。所有 ORM↔API 转换契约。 | dataclasses（无运行时校验）、msgspec（生态弱）。Pydantic v2 是 Pydantic-settings 基石。 |
| `pydantic-settings` | >=2.5 | 从环境变量加载配置，支持 .env、嵌套模型、SecretStr。零样板代码管理 12 项配置。 | 手写 os.getenv 散落各处（违背 DRY）。 |
| `sqlalchemy` | >=2.0 | ORM 但不隐藏 SQL，2.0 风格统一 sync/async API，类型提示完善。 | Tortoise（生态弱）、SQLModel（耦合 Pydantic 限制多）。SQLAlchemy 2.0 是 Python ORM 事实标准。 |
| `asyncpg` | >=0.30 | PostgreSQL 异步驱动，原生协议实现，性能远超 psycopg2。LLM 调用本身是 IO 密集，全链路 async 必要。 | psycopg3-async（兼容性好但慢 30%）。 |
| `pgvector` | >=0.3 | PostgreSQL 向量扩展的 Python 适配，提供 VECTOR 类型与算子。RAG 检索核心，无需独立向量数据库。 | pinecone/weaviate（额外运维成本，违背单租户极简部署）。pgvector 复用现有 PG。 |
| `httpx` | >=0.27 | 同步+异步 HTTP 客户端，用于自研 LLM 客户端调用 OpenAI/Anthropic。API 与 requests 一致但支持 async。 | aiohttp（API 复杂）、requests（仅同步）。httpx 是现代 Python HTTP 事实标准。 |
| `redis` | >=5.2 | Redis 客户端，async 支持完善。用于缓存（Prompt/模型配置）、分布式锁（Agent 执行）、限流。 | aioredis（已合并入 redis-py 5.x）。 |
| `pyjwt` | >=2.8 | JWT 编解码。PyJWT 是活跃维护实现，HS256/RS256/ES256 全支持。认证层核心，无状态 Token。 | python-jose（已停止维护，最后发布 2022；功能重叠但维护停滞）。 |
| `bcrypt` | >=4.0 | 密码哈希，bcrypt 算法直接调用。用户认证必备。 | passlib（1.7.4 与 bcrypt>=4.1 不兼容，且维护停滞；bcrypt 4.x 内置类型注解，直调更轻量）。 |
| `python-multipart` | >=0.0.12 | FastAPI 文件上传（`UploadFile`）依赖。知识库文档上传必需。 | 无（FastAPI 文件上传硬依赖）。 |
| `email-validator` | >=2.1 | Pydantic `EmailStr` 运行期校验依赖（auth 域 `UserCreate`/`LoginRequest`）。原 alpha 代码已用 `EmailStr` 但未声明，现补齐。 | 无（`EmailStr` 硬依赖）。 |

## 开发依赖（dev）

| 依赖 | 版本 | 引入理由 |
|------|------|---------|
| `pytest` | >=8.3 | 测试框架，Eval-Driven 工作流的执行器。 |
| `pytest-asyncio` | >=0.24 | pytest async 支持，所有 service 测试为 async。 |
| `pytest-cov` | >=5.0 | 覆盖率统计，L1 单元测试覆盖率门槛验证。 |
| `ruff` | >=0.7 | Linter + Formatter，Rust 实现 100x 快于 flake8。 |
| `mypy` | >=1.13 | 静态类型检查，"Types as Docs" 原则的强制层。 |
| `alembic` | >=1.13 | SQLAlchemy 官方迁移工具，autogenerate 从 `Base.metadata` 派生迁移，消除 init.sql/ORM 双真源漂移。对应 `specs/migration.spec.md` §3（ORM 单一真源）。原 alpha 阶段用 init.sql，现已按 `specs/migration.spec.md` 引入。 |
| `aiosqlite` | >=0.20 | SQLite async 驱动。L1/L2 测试以 `sqlite+aiosqlite:///:memory:` 跑全异步栈（避免 PG 依赖），`conftest.py` 硬依赖。 | 无（async SQLite 唯一驱动）。 |
| `fakeredis` | >=2.20 | Redis 限流测试用 in-memory fake（security.spec.md§5）。无需真实 Redis 即可验证滑动窗口逻辑、429 响应、per-user keying。 | 手写 ZSET fake 超 50 行门槛。 |

## 明确不引入（Rejected）

| 候选 | 拒绝理由 |
|------|---------|
| `langchain` / `llama-index` | 黑盒依赖，违背 Understanding-First。自研 80 行 LLM 客户端足够。 |
| `celery` | 过重，Redis Stream + asyncio 足以支撑 Agent 异步执行。 |
| `sqlmodel` | 与 Pydantic 耦合限制 ORM 表达力，SQLAlchemy 2.0 + Pydantic schema 分离更清晰。 |
| `fastapi-users` | 认证逻辑 < 50 行，自实现更可控。 |

## 依赖更新策略

- 依赖锁定在主版本（`>=X.Y`），允许 patch/minor 更新
- 每月执行 `pip-audit` 安全扫描
- 重大版本升级必须通过完整 eval 回归
