# AIOps Console — 开发规划 (Roadmap)

> **版本**: v0.1.0-alpha | **维护规则**: 本文件是项目迭代路线图，每次里程碑完成或优先级调整时更新。
> **对齐**: [SPEC.md](SPEC.md) §8 Success Criteria · [agents.md](agents.md) 开发工作流
> **哲学**: Spec-Driven · Eval-Driven · 每个 PR diff < 200 行

---

## 0. 当前状态快照 (2026-07-03)

### 0.1 分支状态

| 分支 | HEAD | 状态 |
|------|------|------|
| `main` | `2e0107d` | ✅ **最新**（当前分支，已含 P2/P3 review 修复） |
| `feat/spec-driven-auth-and-domain-fixes` | `21e6b98` | ⚠️ 落后 main 1 个 commit（review 修复未合并回该分支） |
| `trae/agent-YhMdqP` | `2e0107d` | 与 main 一致（Agent 工作分支） |

> **结论**: 所有开发应基于 `main`。`feat/spec-driven-auth-and-domain-fixes` 已被 main 超越，可归档/删除。

### 0.2 已完成度

| 维度 | 状态 | 证据 |
|------|------|------|
| 后端 6 领域 + auth | ✅ 已实现 | `backend/app/domains/{prompts,agents,knowledge,models,analytics,evals,auth}` 全部 router/service/models |
| 后端 core 模块 | ✅ 已实现 | config / database / jwt / deps / errors/ / llm_client / logging / metrics |
| 前端 6 领域 + shared + views | ✅ 已实现 | `frontend/src/domains/*` + `views/*` + `shared/*` |
| 横切 Specs | ✅ 已就绪 | `specs/{errors,security,testing,migration,deployment,observability}.spec.md` + `openapi.yaml` |
| L1 单元测试 | ✅ 25 后端 + 2 前端文件 | `pytest --cov-fail-under=80` |
| L2 契约测试 | ✅ 已有 | `backend/tests/test_api_contract.py`（schemathesis） |
| L3 E2E | ✅ 4 个 spec | `frontend/e2e/{smoke,models,prompts}.spec.ts` |
| L4 LLM-as-Judge | ✅ 已有 | `backend/app/domains/evals/tests/eval_llm_as_judge.py` |
| CI 流水线 | ✅ 已配置 | `.github/workflows/ci.yml`（lint/mypy/pytest + type-check/build/vitest/playwright） |
| 容器化 | ✅ 已就绪 | `docker-compose.yml` + `ops/Dockerfile.{backend,frontend}` + `nginx.conf` |
| K8s 基础 | ✅ 已就绪 | `ops/k8s/{deployment,ingress}.yaml`（Deployment/Service/StatefulSet/Redis） |

### 0.3 关键缺口（待办来源）

| # | 缺口 | 严重度 | 规范依据 |
|---|------|--------|----------|
| G1 | **无 Alembic 迁移**，4 项已知 schema 漂移未修复 | P0 | `specs/migration.spec.md` §9（eval_rules/eval_judges/eval_cases 缺 ORM，users 无 ORM） |
| G2 | K8s 缺 HPA / PDB，frontend 仅 1 replica，未分离 ConfigMap | P1 | `specs/deployment.spec.md` §8.4 |
| G3 | CI 未含 secret 扫描、L4 阈值门禁、迁移一致性校验 | P1 | `specs/testing.spec.md` + `migration.spec.md` §8 |
| G4 | Redis 滑动窗口限流（100/min，LLM 20/min）落地待核 | P1 | `specs/security.spec.md` |
| G5 | 可观测告警规则未配置（错误率 >5%、P99 >2s、成本阈值） | P2 | `specs/observability.spec.md` |
| G6 | OpenAPI ↔ 代码双向同步流程未固化（gen:api 已有但未入 CI） | P2 | `frontend/SPEC.md` §4.1 |

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

- [ ] **1.1** 引入 Alembic：`alembic.ini` + `migrations/env.py`（从 `Base.metadata` 读 target_metadata）
- [ ] **1.2** 补 `User` ORM 模型（`domains/auth/models.py`），生成首个 baseline 迁移接管 users 表
- [ ] **1.3** 补 `EvalRule` / `EvalJudge` / `EvalCase` ORM 模型（`domains/evals/models.py`），autogenerate 迁移
- [ ] **1.4** `init.sql` 退化为仅扩展 + 种子 + 索引（移除所有 `CREATE TABLE` 业务表）
- [ ] **1.5** CI 增加「ORM vs DB 一致性校验」job（`alembic upgrade head` ↔ `Base.metadata.create_all` diff）
- [ ] **1.6** `migration.spec.md` §10 验收清单全部勾选

**验收**: `alembic upgrade head` / `downgrade -1` 均可执行；CI 一致性校验通过；已知漂移清单清零。

---

## Phase 2 — 质量门禁闭环 (P1)

> 目标：四层测试金字塔全部在 CI 中强制。对应 SPEC §8.2。

- [ ] **2.1** CI 显式运行 L2 schemathesis（当前 `pytest` 隐式包含，需独立 job + 报告）
- [ ] **2.2** L3 E2E 覆盖关键路径「登录 → 创建 Prompt → 版本管理 → 回滚」并针对 `vite build` 产物运行
- [ ] **2.3** L4 LLM-as-Judge 接入 CI，得分 > 0.85 为门禁（未达记录评审，不阻断）
- [ ] **2.4** 前端 Vitest 覆盖率门槛 80% 写入 CI
- [ ] **2.5** OpenAPI 类型生成（`gen:api`）纳入 CI 前置步骤 + diff 校验（G6）

**验收**: CI 流程 `lint → L1 → L2 → L3 → L4 → coverage` 全绿；类型与 OpenAPI 无漂移。

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

- [ ] **5.1 Prompt Studio**: A/B 测试、变量模板渲染
- [ ] **5.2 Agent Orchestrator**: 工作流 DAG（≤50 节点）执行追踪、工具注册面板
- [ ] **5.3 Knowledge Base**: 重排序、RAG 端到端检索质量 eval
- [ ] **5.4 Model Router**: round_robin / least_cost / latency 策略 + fallback 链路
- [ ] **5.5 Analytics**: 按用户/模型/时间维度统计 + 满意度
- [ ] **5.6 Eval Suite**: 回归检测、L4 > 0.85 全量验证

**验收**: 各领域 SPEC.md 中声明的核心能力均有 eval 覆盖且通过。

---

## Phase 6 — 生产化与文档 (GA)

> 目标：v0.1.0 正式发布。

- [ ] **6.1** README 快速开始验证（docker compose up 一键起）
- [ ] **6.2** 部署文档（env vars / K8s / 密钥注入）
- [ ] **6.3** Success Criteria（SPEC §8）全部勾选
- [ ] **6.4** 代码审查报告中遗留 P2/P3 项清零
- [ ] **6.5** 打 tag `v0.1.0`

---

## 工作约定

- 每个 checklist 项 = 1 个 PR，diff < 200 行，引用对应 SPEC 条款。
- 优先级：P0 阻塞所有后续；P1 进入 beta 前 must-have；P2 进入 rc/GA 前完成。
- 完成的 Phase 在本文件标注日期并归档至 git tag。
