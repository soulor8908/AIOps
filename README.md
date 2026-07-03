# AIOps Console

> AI 原生运营控制台 | v0.1.0

AIOps Console 是一个 **AI 原生后台管理系统**，用于管理企业 AI 应用的全生命周期。

## 核心模块

- **Prompt Studio** — 版本化 Prompt 管理、diff、回滚
- **Agent Orchestrator** — 多 Agent 编排、ReAct 循环、工作流 DAG
- **Knowledge Base** — RAG 知识库、文档分块、向量检索
- **Model Router** — 多模型统一路由、负载均衡、Fallback、成本监控
- **Conversation Analytics** — 对话质量分析、Token 消耗追踪
- **Eval Suite** — 自动化评估、LLM-as-judge、回归检测

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 · Alembic |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · Tailwind CSS |
| 数据库 | PostgreSQL 16 + pgvector · Redis 7 |
| AI 层 | 自研轻量 LLM 客户端（零框架） |
| 部署 | Docker · Kubernetes · Prometheus · nginx |

## 快速开始

### 前置条件

- Docker 24+ 与 Docker Compose v2
- （可选）OpenAI 或 Anthropic API Key，用于 LLM / Embedding 能力

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少设置 SECRET_KEY；按需填入 OPENAI_API_KEY / ANTHROPIC_API_KEY
```

### 2. 一键启动开发环境

```bash
docker compose up -d
```

启动后：
- PostgreSQL（pgvector）+ Redis 自动健康检查就绪
- 后端 lifespan 自动 `create_all` 建表（幂等）
- 后端文档：<http://localhost:8000/docs>
- 前端界面：<http://localhost:5173>

> 生产部署应使用 Alembic 迁移而非 `create_all`，见 [DEPLOYMENT.md](DEPLOYMENT.md)。

### 3. 创建首个管理员

系统无内置种子用户，启动后通过 API 注册：

```bash
# 注册（首个用户）
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","username":"admin","password":"Admin123!"}'

# 登录获取 JWT
curl -X POST http://localhost:8000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"Admin123!"}'
```

> 生产环境需将首个用户提升为 admin（直接更新 DB `users.is_admin = true`，或经运维流程）。

### 4. 应用数据库迁移（生产 / 持久化环境）

```bash
# 进入 backend 容器执行 Alembic 迁移
docker compose exec backend alembic upgrade head
```

## 项目结构

```
AIOps/
├── backend/              # FastAPI 后端
│   ├── app/              # 应用代码（core + domains）
│   ├── migrations/       # Alembic 迁移
│   ├── tests/            # L1 单元 + L2 契约测试
│   └── pyproject.toml
├── frontend/             # Vue 3 前端
│   ├── src/domains/      # 六大领域 UI
│   └── e2e/              # L3 E2E 测试
├── specs/                # OpenAPI 3.1 + 横切 spec
├── ops/                  # Dockerfile / K8s / nginx / Prometheus
├── docker-compose.yml
├── SPEC.md               # 项目总规格
├── DEPLOYMENT.md         # 部署指南
└── agents.md             # Agent 工作流配置
```

## 开发工作流

遵循 **Spec → Eval → Code** 三段式（[agents.md](agents.md)）：

1. 阅读相关 `SPEC.md` / `specs/*.spec.md`
2. 根据 SPEC 编写 eval（测试先于代码）
3. 实现代码，引用 SPEC 条款编号
4. 运行 eval（L1-L3 必须 100% 通过，L4 > 0.85）
5. 提交 PR（diff < 200 行）

### 本地开发

```bash
# 后端
cd backend && pip install -e ".[dev]"
pytest                    # L1 单元测试
ruff check . && mypy app  # lint + 类型检查

# 前端
cd frontend && npm ci
npm run dev               # 开发服务器
npm run test:run          # Vitest 单元测试
npm run e2e               # Playwright E2E
npm run gen:api           # 从 specs/openapi.yaml 生成类型
```

## 测试金字塔

| 层级 | 工具 | 范围 | CI 门禁 |
|------|------|------|---------|
| L1 单元 | pytest / Vitest | 后端 core+domains / 前端组件 | 覆盖率 ≥ 80%，阻断 |
| L2 契约 | schemathesis | OpenAPI ↔ 实现一致 | 阻断 |
| L3 E2E | Playwright | 登录 → Prompt → 版本 → 回滚 | 阻断 |
| L4 LLM-as-Judge | pytest + LLM | 输出质量语义评估 | 得分 > 0.85，非阻断 |

## 部署

生产部署（K8s / 密钥注入 / HPA / 可观测）见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## License

Proprietary — Internal use only.
