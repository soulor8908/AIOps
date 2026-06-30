# AIOps Console — 后端规格 (SPEC.md)

> **版本**: v0.1.0-alpha | **对齐**: 项目总 SPEC.md v0.1.0
> **技术栈**: Python 3.12 · FastAPI 0.115 · Pydantic v2.9 · SQLAlchemy 2.0 · pgvector · Redis

---

## 1. 架构原则

1. **Flat > Deep** — `app/domains/{领域}/` 扁平自包含，每个领域 = models + router + service + tests
2. **Types as Docs** — Pydantic schema 即 API 契约，ORM model 即数据契约，禁止冗余注释
3. **Service 纯函数** — `service.py` 是无副作用纯函数（接收 session 与 DTO，返回 DTO）
4. **async/await 全链路** — DB 驱动 asyncpg，HTTP 客户端 httpx.AsyncClient，无同步阻塞点
5. **自研优先** — LLM 客户端、分块器、判官均自研 < 120 行，禁止 langchain 黑盒
6. **Eval-Driven** — 每个领域必须有 `tests/test_*.py`，L1 单元测试覆盖 service 纯函数

## 2. 模块清单

```
app/
├── __init__.py
├── main.py              # FastAPI 入口（< 100 行）：lifespan + CORS + router + /health
├── api.py               # 路由聚合，一行一个 domain
├── core/
│   ├── __init__.py
│   ├── config.py        # Pydantic Settings，12 个字段
│   ├── database.py      # async engine + sessionmaker + Base + init_db
│   ├── security.py      # JWT + OAuth2 + passlib（极简）
│   ├── llm_client.py    # 自研 LLM 客户端（~80 行）：openai/anthropic/local
│   └── exceptions.py    # 全局异常层级
└── domains/
    ├── prompts/    # Prompt Studio
    ├── agents/     # Agent Orchestrator（含 executor.py）
    ├── knowledge/  # Knowledge Base（含 chunker.py + embedder.py）
    ├── models/     # Model Router
    ├── analytics/  # Conversation Analytics
    └── evals/      # Eval Suite（含 judge.py）
```

## 3. 领域契约

### 3.1 Prompt Studio (`/api/v1/prompts`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/prompts` | 列表，支持 `q` / `limit` / `offset` |
| POST | `/prompts` | 创建 Prompt（含初始 version） |
| GET | `/prompts/{id}` | 获取详情 |
| PUT | `/prompts/{id}` | 更新元信息 |
| DELETE | `/prompts/{id}` | 软删除 |
| GET | `/prompts/{id}/versions` | 版本列表 |
| POST | `/prompts/{id}/versions` | 新增版本 |
| POST | `/prompts/{id}/versions/{vid}/rollback` | 回滚到指定版本 |
| GET | `/prompts/{id}/diff?from=v1&to=v2` | 版本 diff |

- **约束**: 单 Prompt 最大 100 版本，内容 ≤ 64KB
- **ORM**: `Prompt`（name, description, current_version_id, versions 关系）+ `PromptVersion`（prompt_id, version_num, content, variables）

### 3.2 Agent Orchestrator (`/api/v1`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agents` | Agent 列表 |
| POST | `/agents` | 创建 Agent |
| GET | `/agents/{id}` | Agent 详情 |
| POST | `/agents/{id}/execute` | 执行 Agent（ReAct 循环） |
| GET | `/workflows` | 工作流列表 |
| POST | `/workflows` | 创建工作流 |
| POST | `/workflows/{id}/execute` | 执行工作流 DAG |

- **约束**: DAG 最大 50 节点，单次执行最大 10 轮
- **executor.py**: `AgentExecutor.run()` 实现 ReAct 循环（观察→思考→行动），`_parse_tool_calls` 解析 LLM 输出中的工具调用

### 3.3 Knowledge Base (`/api/v1/knowledge-bases`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/knowledge-bases` | 知识库列表 |
| POST | `/knowledge-bases` | 创建知识库 |
| GET | `/knowledge-bases/{id}` | 知识库详情 |
| POST | `/knowledge-bases/{id}/documents` | 上传文档（multipart） |
| POST | `/knowledge-bases/{id}/search` | 向量检索 |

- **约束**: 单文档 ≤ 50MB，向量维度 1536
- **chunker.py**: 固定长度分块（支持 chunk_size + overlap）
- **embedder.py**: 调用 OpenAI embedding API，失败回退零向量

### 3.4 Model Router (`/api/v1/models`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/models` | 模型配置列表 |
| POST | `/models` | 新增配置 |
| GET | `/models/{alias}` | 按别名获取 |
| PUT | `/models/{alias}` | 更新配置 |
| DELETE | `/models/{alias}` | 删除配置 |
| POST | `/models/{alias}/chat` | 走该模型对话（含 fallback） |

- **路由策略**: direct / round_robin / least_cost / latency
- **fallback**: primary 失败按 priority 降序尝试

### 3.5 Conversation Analytics (`/api/v1/analytics`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/analytics/conversations` | 对话列表 |
| GET | `/analytics/conversations/{id}` | 对话详情（含 messages） |
| GET | `/analytics/dashboard` | 仪表盘指标 |

### 3.6 Eval Suite (`/api/v1/evals`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/evals` | eval 列表 |
| POST | `/evals` | 创建 eval |
| GET | `/evals/{id}` | eval 详情 |
| POST | `/evals/{id}/run` | 执行 eval（同步） |

- **judge.py**: `judge_exact` / `judge_contains` / `judge_llm` / `judge_semantic`
- **eval_runs**: UUID 主键，status: pending / running / passed / failed

## 4. 数据库

- 异步 engine（`asyncpg`），sessionmaker `AsyncSession`
- `Base = declarative_base()`（SQLAlchemy 2.0 风格 `DeclarativeBase`）
- 所有 ORM 继承 `app.core.database.Base`
- 向量字段使用 `pgvector.sqlalchemy.Vector(1536)`
- `init_db()`: 建表 + 索引（开发期，生产用 alembic）

## 5. 安全

- JWT (HS256) + OAuth2PasswordBearer
- `create_access_token` / `verify_token` / `hash_password` / `verify_password`
- Token 默认 30 分钟过期
- 密码 bcrypt via passlib

## 6. LLM 客户端

自研 ~80 行，封装：
- `Message` (role, content)
- `LLMConfig` (provider, model, api_key, base_url, temperature, max_tokens)
- `LLMResponse` (content, tool_calls, usage, raw)
- `LLMClient` (chat(messages) -> LLMResponse, _call_openai 完整, _call_anthropic, _call_local)
- provider: `openai` / `anthropic` / `local`

## 7. 错误处理

- `NotFoundError(404)` / `ValidationError(422)` / `AuthenticationError(401)`
- `AuthorizationError(403)` / `ConflictError(409)` / `LLMError(502)`
- 全局异常处理器在 `main.py` 注册，统一返回 `{ "error": ..., "detail": ... }`

## 8. 测试

- `pytest-asyncio`，`asyncio_mode = "auto"`
- 每领域 `tests/test_*.py` 覆盖 service 纯函数
- 使用 SQLite in-memory 或 mock session（不依赖真实 PG）

## 9. 配置（环境变量）

| 字段 | 默认 | 说明 |
|------|------|------|
| `APP_VERSION` | 0.1.0-alpha | 版本 |
| `DEBUG` | true | 调试模式 |
| `DATABASE_URL` | postgresql+asyncpg://aiops:aiops@localhost:5432/aiops | DB |
| `REDIS_URL` | redis://localhost:6379 | Redis |
| `SECRET_KEY` | change-me | JWT 密钥 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 30 | Token 过期 |
| `CORS_ORIGINS` | ["*"] | CORS 白名单 |
| `DEFAULT_LLM_PROVIDER` | openai | 默认 provider |
| `DEFAULT_LLM_MODEL` | gpt-4o-mini | 默认模型 |
| `OPENAI_API_KEY` | "" | OpenAI Key |
| `ANTHROPIC_API_KEY` | "" | Anthropic Key |

---

> 本 spec 由 Agentic Engineering 流程维护。每次后端架构变更必须先更新本 SPEC.md。
