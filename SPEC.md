# AIOps Console — 项目总规格

> **版本**: v0.1.0 | **日期**: 2026-07-03
> **哲学**: Agentic Engineering · Spec-Driven · Minimalism · Understanding-First

---

## 0. 架构哲学

遵循 Karpathy **Agentic Engineering** 哲学：

| 原则 | 实践 |
|------|------|
| **Spec即契约** | 每个模块先有 SPEC.md 再写测试再写代码 |
| **Minimalism** | 引入依赖前通过50行测试，能用50行内实现就不引入库 |
| **Understanding-First** | AI生成代码后必须能读懂，禁止黑盒依赖 |
| **Eval-Driven** | 每个功能必须有可自动验证的 eval |
| **Context as Code** | agents.md 和 SPEC.md 是 LLM 的上下文程序 |
| **Flat > Deep** | 目录扁平，模块自包含，避免深层抽象 |
| **Types as Docs** | Pydantic/TypeScript 类型即文档，零注释冗余 |

## 1. 项目定位

AIOps Console 是一个 **AI 原生后台管理系统**，用于管理企业 AI 应用的全生命周期。

### 1.1 核心模块

- **Prompt Studio** — 版本化 Prompt 管理、A/B 测试、变量模板
- **Agent Orchestrator** — 多 Agent 编排、工作流 DAG、工具注册
- **Knowledge Base** — RAG 知识库、文档分块、向量检索、重排序
- **Model Router** — 多模型统一路由、负载均衡、Fallback、成本监控
- **Conversation Analytics** — 对话质量分析、用户满意度、Token 追踪
- **Eval Suite** — 自动化评估、LLM-as-judge、回归检测

### 1.2 目标用户

- AI 应用开发团队（5-30 人）
- 需要管理多个 LLM 项目的技术负责人
- 需要 Prompt 版本控制和 A/B 测试的 AI 产品经理

### 1.3 非目标

- 不是通用低代码平台
- 不是模型训练平台（只做推理路由）
- 不是多租户 SaaS（单租户企业部署优先）

## 2. 技术选型

### 2.1 后端

| 技术 | 版本 | 理由 |
|------|------|------|
| Python | 3.12 | AI 生态原生 |
| FastAPI | 0.115 | 自动生成 OpenAPI，Pydantic v2 原生 |
| Pydantic v2 | 2.9 | Rust 核心，性能 10x，类型严格 |
| SQLAlchemy 2.0 | 2.0 | ORM 但不隐藏 SQL，支持 async |
| pgvector | 0.7 | PostgreSQL 向量扩展，无需额外数据库 |
| Redis | 7.2 | 缓存 + 消息队列 + 分布式锁 |
| pytest | 8.3 | 测试框架，async 支持完善 |

### 2.2 前端

| 技术 | 版本 | 理由 |
|------|------|------|
| Vue 3 | 3.5 | 响应式直觉友好，script setup 简洁 |
| TypeScript | 5.6 | 严格模式，与 Pydantic 类型可双向推导 |
| Vite | 8 | Rolldown 引擎，构建速度 10x |
| Pinia | 3 | 官方状态管理，Setup Store 语法简洁 |
| Tailwind CSS | 3.4 | 原子化 CSS，无需设计系统即可保持统一 |
| shadcn-vue | 0.11 | 可复制组件，非 npm 依赖，完全可控 |

### 2.3 AI 层

自研轻量客户端（约 80 行），直接封装 OpenAI/Anthropic API，零隐藏逻辑。

## 3. 目录结构

```
AIOps/
├── SPEC.md                          # 项目总规格
├── agents.md                        # Agent 工作流配置
├── docker-compose.yml               # 本地开发环境
├── backend/
│   ├── SPEC.md                      # 后端规格
│   ├── DEPENDENCY.md                # 依赖声明与理由
│   ├── pyproject.toml               # Poetry 配置
│   ├── Dockerfile
│   ├── init.sql                     # 数据库初始化
│   └── app/
│       ├── main.py                  # FastAPI 入口
│       ├── api.py                   # 路由聚合
│       ├── core/                    # 核心模块
│       └── domains/                 # 领域层（扁平自包含）
│           ├── auth/                # 认证授权（User ORM、JWT、RBAC）
│           ├── prompts/
│           ├── agents/
│           ├── knowledge/
│           ├── models/
│           ├── analytics/
│           └── evals/
├── frontend/
│   ├── SPEC.md                      # 前端规格
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── domains/                 # 与后端 domains 严格对齐
│       ├── shared/                  # 共享层
│       └── views/                   # 页面
├── specs/                           # 横切关注点 + 契约规范
│   ├── openapi.yaml                 # OpenAPI 3.1 规范
│   ├── errors.spec.md               # 错误处理（统一格式/状态码/AppError）
│   ├── security.spec.md             # 安全（JWT/RBAC/CORS/限流/密钥）
│   ├── testing.spec.md              # 测试（四层金字塔/覆盖率门槛/CI 门禁）
│   ├── migration.spec.md            # 数据库迁移（ORM 真源/Alembic/漂移）
│   ├── deployment.spec.md           # 部署（镜像/K8s/健康检查）
│   └── observability.spec.md        # 可观测性（日志/追踪/指标/告警）
└── ops/
    ├── Dockerfile.backend
    ├── Dockerfile.frontend
    └── k8s/                         # K8s manifests
```

## 4. 核心领域概要

详见各领域 `SPEC.md` 及 `specs/openapi.yaml`。

| 领域 | 核心能力 | 关键约束 |
|------|---------|---------|
| Prompt Studio | CRUD + 版本管理 + 回滚 + diff | 单 Prompt 最大 100 版本，内容 64KB |
| Agent Orchestrator | Agent 定义 + DAG 编排 + 执行追踪 | DAG 最大 50 节点，单次最大 10 轮 |
| Knowledge Base | 文档上传 + 分块 + 向量检索 + RAG | 单文档 50MB，向量维度 1536 |
| Model Router | 多模型配置 + 路由 + Fallback + 成本 | 路由策略：direct/round_robin/least_cost/latency |
| Conversation Analytics | 对话记录 + Token 统计 + 质量评分 | 按用户/模型/时间维度统计 |
| Eval Suite | 规则测试 + LLM-as-judge + 回归检测 | L4 得分 > 0.85 |

## 5. 开发工作流

**Spec → Eval → Code** 三段式：

1. **Spec 定义** — 产出 SPEC.md、API.spec.yaml、DATA.spec.md
2. **Eval 编写** — L1 单元 / L2 契约 / L3 E2E / L4 LLM-as-Judge
3. **Code 实现** — 引用 Spec 条款，eval 通过，diff < 200 行

## 6. 部署

- **本地开发**：docker compose up（PostgreSQL + Redis + Backend + Frontend）
- **生产部署**：K8s 极简 manifest（见 ops/k8s/）

## 7. 横切关注点规范

跨领域的横切契约统一收敛到 `specs/` 目录，作为全栈强制规范。各领域 SPEC.md 与 agents.md 均须引用并遵循。

| 关注点 | 规范文件 | 核心约束 |
|--------|----------|----------|
| 错误处理 | [`specs/errors.spec.md`](specs/errors.spec.md) | 统一响应 `{error, message, detail}`；HTTP 状态码 400/401/403/404/409/422/429/500；`AppError` 基类 + 子类；`RequestValidationError` 改写为统一格式；全局 500 兜底；前端 `client.ts` 统一抛 `ApiError` |
| 安全 | [`specs/security.spec.md`](specs/security.spec.md) | JWT Bearer（默认 24h）；RBAC（admin/user）；CORS 禁止 `*`+credentials；Redis 滑动窗口限流（默认 100/min，LLM 20/min）；bcrypt 密码；文件上传白名单 + 50MB；API Key 仅服务端；Secret 经 K8s 注入 |
| 测试 | [`specs/testing.spec.md`](specs/testing.spec.md) | 四层金字塔（L1 pytest/L2 schemathesis/L3 Playwright/L4 LLM-as-Judge）；L1 覆盖率 ≥ 80%（`--cov-fail-under=80`）；L1–L3 必须 100% 通过阻断合并，L4 > 0.85；factory pattern 测试数据 |
| 数据库迁移 | `specs/migration.spec.md` | ORM（`Base.metadata`）为单一真源；Alembic autogenerate；`init.sql` 仅扩展/种子/索引；CI 一致性校验；已知漂移修复清单（eval_* 表、users ORM） |
| 部署 | `specs/deployment.spec.md` | 多阶段镜像 + 非 root（UID 1000）+ HEALTHCHECK；前端 nginx 托管 `dist/` 禁 dev server；K8s 2+ replicas + HPA + PDB + probe；Secret/ConfigMap 分离；`/health` 端点 |
| 可观测性 | `specs/observability.spec.md` | JSON 结构化日志（timestamp/level/logger/message/request_id/user_id/latency_ms）；每请求 request_id 追踪；指标（请求数/延迟/错误率/LLM token 与成本）；告警（错误率 >5%、P99 >2s、成本超阈值）；前端监控 |

> 凡涉及上述关注点的实现，必须先读对应 `specs/*.spec.md`，代码中引用其条款编号。

## 8. Success Criteria

项目交付与每次迭代的成功标准（可量化、可验收）。v0.1.0 GA 验收结果如下（证据见各条标注与 `ROADMAP.md`）：

### 8.1 功能完整性
- [x] 六大核心领域（Prompt Studio、Agent Orchestrator、Knowledge Base、Model Router、Conversation Analytics、Eval Suite）均具备 CRUD 与各自核心能力，且与 `specs/openapi.yaml` 契约一致。
  - **达成**：Phase 5 六领域核心能力 eval 全部通过（`test_prompts_versioning.py` / `test_agents_execution.py` / `test_knowledge_pipeline.py` / `test_models_routing.py` / `test_analytics_aggregation.py` / `test_eval_suite.py`）；L2 schemathesis 契约对齐 `openapi.yaml`。
- [x] 关键用户路径（登录 → 创建 Prompt → 版本管理 → 回滚）E2E 通过。
  - **达成**：`frontend/e2e/prompts.spec.ts` 覆盖登录→创建→版本→回滚全路径，针对 `vite build` 产物运行。

### 8.2 质量门槛
- [x] **L1 单元测试**：覆盖率 ≥ 80%，100% 通过（`pytest --cov-fail-under=80`）。
  - **达成**：`backend/pyproject.toml` `--cov-fail-under=80`；CI `backend-test` job 强制；全量 421 passed。
- [x] **L2 契约测试**：schemathesis 覆盖所有端点，100% 通过。
  - **达成**：CI 独立 `contract-test` job 运行 `tests/test_api_contract.py`。
- [x] **L3 E2E 测试**：关键路径 100% 通过，针对 `vite build` 产物运行。
  - **达成**：CI `frontend-test` job `npm run e2e` 对 `dist/` 运行 Playwright。
- [x] **L4 LLM-as-Judge**：得分 > 0.85。
  - **达成**：CI `llm-judge` job 运行 `eval_llm_as_judge.py`；阈值 > 0.85（`continue-on-error` 非阻断，无 `OPENAI_API_KEY` 时 skipif 跳过）。
- [x] **错误一致性**：所有非 2xx 响应符合 `errors.spec.md` 统一格式，无 `{detail:[...]}` 残留。
  - **达成**：`app/core/errors/` 全局异常处理器改写 `RequestValidationError` 为 `{error, message, detail}`；L2 契约校验状态码 + schema。

### 8.3 安全基线
- [x] 认证授权（JWT + RBAC）落地，401/403 边界正确。
  - **达成**：`app/core/jwt.py` + `app/core/deps.py` RBAC；Phase 3.2 回归测试覆盖每个受保护端点。
- [x] 生产 CORS 无 `*` + credentials；LLM API Key 不触达前端；CI secret 扫描通过。
  - **达成**：`ops/k8s/deployment.yaml` ConfigMap `CORS_ORIGINS` 显式列举；LLM Key 走 `aiops-secrets` Secret；CI `secret-scan` job（gitleaks）阻断合并。

### 8.4 可部署性
- [x] 镜像多阶段、非 root、含 HEALTHCHECK；前端生产由 nginx 托管静态文件。
  - **达成**：`ops/Dockerfile.{backend,frontend}` 多阶段 + UID 1000 + HEALTHCHECK；前端 nginx 托管 `dist/`（`frontend/nginx.conf`）。
- [x] K8s 部署：backend/frontend 均 2+ replicas、HPA、PDB、liveness/readiness probe、资源限制就绪；`/health` 返回 status + version。
  - **达成**：`ops/k8s/deployment.yaml` backend/frontend 各 2 replicas + HPA（backend 2-6 / frontend 2-4）+ PDB `minAvailable: 1` + probe + resources；`/health` 依赖感知返回 `{status, version, checks}`。

### 8.5 可观测性
- [x] 生产日志为结构化 JSON，含 request_id 贯穿链路。
  - **达成**：`app/core/logging.py` JSON formatter（timestamp/level/logger/message/request_id/user_id/latency_ms）；`test_main.py::test_request_id_propagates_to_request_log` 验证。
- [x] 请求数/延迟/错误率/LLM token 与成本指标已采集；错误率 >5%、P99 >2s、LLM 成本超阈值告警已配置。
  - **达成**：`app/core/metrics.py` Prometheus 指标；`ops/k8s/` 告警规则（错误率 >5% / P99 >2s / 成本阈值）Phase 4.5 落地。

### 8.6 工程纪律
- [x] 每个 PR：diff < 200 行，引用 SPEC 条款，eval 通过。
  - **达成**：`agents.md` 工作流强制；Phase 1-6 各 batch commit 均遵循。
- [x] 数据库 schema 无漂移（ORM vs DB 一致性校验通过）。
  - **达成**：`backend/migrations/` Alembic（0001 建表 + 0002 种子）；CI `migration-consistency` job 跑 `check_schema_consistency.py`（PG + pgvector upgrade → 校验 → downgrade → replay）。
- [x] 横切关注点变更先更新 `specs/*.spec.md`，再更新 eval，再更新代码。
  - **达成**：`specs/{errors,security,testing,migration,deployment,observability}.spec.md` 验收清单全部 `[x]`；Phase 1-6 各横切变更均先更新 spec。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次架构变更必须先更新 SPEC.md，再更新 eval，再更新代码。
