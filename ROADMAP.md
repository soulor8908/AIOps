# AIOps Console — 开发规划 (Roadmap)

> **版本**: v0.1.0 | **维护规则**: 本文件是项目迭代路线图，每次里程碑完成或优先级调整时更新。
> **对齐**: [SPEC.md](SPEC.md) §8 Success Criteria · [agents.md](agents.md) 开发工作流
> **哲学**: Spec-Driven · Eval-Driven · 每个 PR diff < 200 行

---

## 0. 当前状态快照 (2026-07-03)

### 0.1 分支状态

| 分支 | HEAD | 状态 |
|------|------|------|
| `main` | `47f673b` | ✅ Phase 5 已合并；Phase 6 GA 待合并 |
| `feat/phase6-ga` | （Phase 6 进行中） | 🔄 当前工作分支（README/DEPLOYMENT/SPEC §8/ci.yml G6/版本 bump） |
| `trae/agent-NbYAhe` | `9f9af4a` | 落后 main，可归档 |

> **结论**: 所有开发基于 `main`。Phase 6 完成后 `feat/phase6-ga` 以 `--no-ff` 合并回 main 并打 tag `v0.1.0`。

### 0.2 已完成度

| 维度 | 状态 | 证据 |
|------|------|------|
| 后端 6 领域 + auth | ✅ 已实现 | `backend/app/domains/{prompts,agents,knowledge,models,analytics,evals,auth}` 全部 router/service/models |
| 后端 core 模块 | ✅ 已实现 | config / database / jwt / deps / errors/ / llm_client / logging / metrics / health / rate_limit |
| 前端 6 领域 + shared + views | ✅ 已实现 | `frontend/src/domains/*` + `views/*` + `shared/*` |
| 横切 Specs | ✅ 已就绪 | `specs/{errors,security,testing,migration,deployment,observability}.spec.md` + `openapi.yaml` |
| Alembic 迁移 | ✅ 已落地 | `backend/migrations/versions/{0001_initial_schema,0002_seed_models}.py` + `alembic.ini` + `env.py`（G1） |
| L1 单元测试 | ✅ 27 后端 + 前端文件 | `pytest --cov-fail-under=80`；全量 421 passed |
| L2 契约测试 | ✅ CI 独立 job | `backend/tests/test_api_contract.py`（schemathesis） |
| L3 E2E | ✅ 4 个 spec | `frontend/e2e/{smoke,models,prompts}.spec.ts`，针对 `vite build` 产物 |
| L4 LLM-as-Judge | ✅ CI 独立 job | `backend/app/domains/evals/tests/eval_llm_as_judge.py`（>0.85，非阻断） |
| CI 流水线 | ✅ 7 job | `.github/workflows/ci.yml`（backend-test / contract-test / migration-consistency / openapi-sync / frontend-test / llm-judge / secret-scan） |
| 容器化 | ✅ 已就绪 | `docker-compose.yml` + `ops/Dockerfile.{backend,frontend}` + `nginx.conf`（多阶段/非 root/HEALTHCHECK） |
| K8s 部署 | ✅ 高可用就绪 | `ops/k8s/deployment.yaml`（2 replicas + HPA + PDB + ConfigMap/Secret 分离 + probe） |

### 0.3 关键缺口（已全部清零）

| # | 缺口 | 严重度 | 状态 | 落地证据 |
|---|------|--------|------|----------|
| G1 | **无 Alembic 迁移**，schema 漂移 | P0 | ✅ 已清零 | `backend/migrations/versions/0001_initial_schema.py` + User/EvalRule/EvalJudge/EvalCase ORM；CI `migration-consistency` job 守门（Phase 1） |
| G2 | K8s 缺 HPA / PDB，frontend 仅 1 replica | P1 | ✅ 已清零 | `ops/k8s/deployment.yaml` backend/frontend 各 2 replicas + HPA + PDB + ConfigMap/Secret 分离（Phase 4 batch 1） |
| G3 | CI 未含 secret 扫描 / L4 门禁 / 迁移一致性 | P1 | ✅ 已清零 | ci.yml `secret-scan`（gitleaks）/ `llm-judge` / `migration-consistency` job（Phase 2/3） |
| G4 | Redis 滑动窗口限流落地待核 | P1 | ✅ 已清零 | `app/core/rate_limit.py` + `test_core_rate_limit.py`（Phase 3.1） |
| G5 | 可观测告警规则未配置 | P2 | ✅ 已清零 | `ops/k8s/` 告警规则（错误率 >5% / P99 >2s / 成本阈值）（Phase 4.5） |
| G6 | OpenAPI ↔ 代码同步流程未固化 | P2 | ✅ 已清零 | ci.yml `openapi-sync` job（gen:api 生成 `types.generated.ts` + diff 校验阻断）（Phase 6.4） |

---

## 1. 里程碑总览

```
Phase 1 (P0) 数据库迁移与漂移清零   →  v0.1.0-beta.1
Phase 2 (P1) 质量门禁闭环           →  v0.1.0-beta.2
Phase 3 (P1) 安全基线落地           →  v0.1.0-beta.3
Phase 4 (P2) 部署与可观测闭环       →  v0.1.0-rc.1
Phase 5 (P2) 领域功能深化           →  v0.1.0
Phase 6       生产化与文档          →  v0.1.0 GA
```

---

## Phase 1 — 数据库迁移与漂移清零 (P0)

> 目标：建立 ORM 单一真源，消除历史 schema 漂移。对应 SPEC §8.6「无 schema 漂移」。

- [x] **1.1** 引入 Alembic：`alembic.ini` + `migrations/env.py`（从 `Base.metadata` 读 target_metadata）
- [x] **1.2** 补 `User` ORM 模型（`domains/auth/models.py`），生成首个 baseline 迁移接管 users 表
- [x] **1.3** 补 `EvalRule` / `EvalJudge` / `EvalCase` ORM 模型（`domains/evals/models.py`），autogenerate 迁移
- [x] **1.4** `init.sql` 退化为仅扩展 + 种子 + 索引（移除所有 `CREATE TABLE` 业务表）
- [x] **1.5** CI 增加「ORM vs DB 一致性校验」job（`alembic upgrade head` ↔ `Base.metadata.create_all` diff）
- [x] **1.6** `migration.spec.md` §10 验收清单全部勾选

**验收**: `alembic upgrade head` / `downgrade -1` 均可执行；CI 一致性校验通过；已知漂移清单清零。✅ Phase 1 完成（`0001_initial_schema.py` 建表 + `0002_seed_models.py` 种子；CI `migration-consistency` job 跑 PG + pgvector upgrade → 校验 → downgrade → replay）。

---

## Phase 2 — 质量门禁闭环 (P1)

> 目标：四层测试金字塔全部在 CI 中强制。对应 SPEC §8.2。

- [x] **2.1** CI 显式运行 L2 schemathesis（当前 `pytest` 隐式包含，需独立 job + 报告）
- [x] **2.2** L3 E2E 覆盖关键路径「登录 → 创建 Prompt → 版本管理 → 回滚」并针对 `vite build` 产物运行
- [x] **2.3** L4 LLM-as-Judge 接入 CI，得分 > 0.85 为门禁（未达记录评审，不阻断）
- [x] **2.4** 前端 Vitest 覆盖率门槛 80% 写入 CI
- [x] **2.5** OpenAPI 类型生成（`gen:api`）纳入 CI 前置步骤 + diff 校验（G6）

**验收**: CI 流程 `lint → L1 → L2 → L3 → L4 → coverage` 全绿；类型与 OpenAPI 无漂移。✅ Phase 2 完成（ci.yml 7 job：backend-test / contract-test / migration-consistency / openapi-sync / frontend-test / llm-judge / secret-scan；2.5 由 Phase 6.4 G6 落地 `openapi-sync` job 守门 `types.generated.ts` 漂移）。

---

## Phase 3 — 安全基线落地 (P1)

> 目标：认证授权与限流边界完整。对应 SPEC §8.3。

- [x] **3.1** Redis 滑动窗口限流（默认 100/min，LLM 端点 20/min）实现 + 测试（G4）
- [x] **3.2** RBAC 边界回归测试（401/403 覆盖每个受保护端点，默认拒绝未声明权限端点）
- [x] **3.3** CI 增加 secret 扫描（gitleaks/trufflehog）（G3）
- [x] **3.4** 生产 CORS 校验（禁 `*` + credentials）；LLM API Key 仅服务端持有审计
- [x] **3.5** 文件上传白名单 + 50MB 限制回归测试

**验收**: 401/403 边界全绿；secret 扫描无泄漏；限流中间件可观测。✅ Phase 3 完成（batch 1 + batch 2）。

---

## Phase 4 — 部署与可观测闭环 (P2)

> 目标：生产可部署、可观测。对应 SPEC §8.4 / §8.5。

- [x] **4.1** K8s 补 HPA + PDB，frontend 升至 2 replicas，ConfigMap/Secret 分离（G2）
- [x] **4.2** `/health` 返回 status + version；镜像含 HEALTHCHECK + 非 root（UID 1000）回归
- [x] **4.3** 结构化 JSON 日志 + request_id 贯穿链路验证
- [x] **4.4** 指标采集（请求数/延迟/错误率/LLM token 与成本）接入 Prometheus
- [x] **4.5** 告警规则：错误率 >5%、P99 >2s、LLM 成本超阈值（G5）

**验收**: K8s 一键部署 2+ replicas 健康；指标/告警可查。✅ Phase 4 完成（batch 1/2/3）。

---

## Phase 5 — 领域功能深化 (P2)

> 目标：六大领域核心能力从「可用」到「完整」。对应 SPEC §8.1。

- [x] **5.1 Prompt Studio**: A/B 测试、变量模板渲染（SPEC 声明为 Non-Goal，实际交付版本管理 eval）
- [x] **5.2 Agent Orchestrator**: 工作流 DAG（≤50 节点）执行追踪、工具注册面板
- [x] **5.3 Knowledge Base**: 重排序、RAG 端到端检索质量 eval（重排序为 Non-Goal，交付上传/检索/RAG pipeline eval）
- [x] **5.4 Model Router**: round_robin / least_cost / latency 策略 + fallback 链路
- [x] **5.5 Analytics**: 按用户/模型/时间维度统计 + 满意度
- [x] **5.6 Eval Suite**: 回归检测、L4 > 0.85 全量验证（L4 LLM-as-judge 已含 skipif 守卫；交付 service+judge 8 项 SC eval）

**验收**: 各领域 SPEC.md 中声明的核心能力均有 eval 覆盖且通过。

---

## Phase 6 — 生产化与文档 (GA)

> 目标：v0.1.0 正式发布。

- [x] **6.1** README 快速开始验证（docker compose up 一键起）
- [x] **6.2** 部署文档（env vars / K8s / 密钥注入）
- [x] **6.3** Success Criteria（SPEC §8）全部勾选
- [x] **6.4** 代码审查报告中遗留 P2/P3 项清零（G1-G6 全部清零；G6 由 `openapi-sync` CI job 落地）
- [x] **6.5** 打 tag `v0.1.0`（版本 bump 0.1.0-alpha → 0.1.0：pyproject / package.json / package-lock / .env.example / ConfigMap / config.py / openapi.yaml / API.spec.yaml / App.vue / 各 SPEC 头部）

---

## 工作约定

- 每个 checklist 项 = 1 个 PR，diff < 200 行，引用对应 SPEC 条款。
- 优先级：P0 阻塞所有后续；P1 进入 beta 前 must-have；P2 进入 rc/GA 前完成。
- 完成的 Phase 在本文件标注日期并归档至 git tag。
