# 部署指南 (Deployment)

> 对齐 `specs/deployment.spec.md`（规范）与 `ops/`（manifests）。本文档为**操作指南**。
> 版本：v0.1.0

## 1. 镜像构建

镜像位于 `ops/Dockerfile.{backend,frontend}`，均满足基线（`deployment.spec.md` §2）：

- **多阶段构建**：builder（编译/装依赖）→ runtime（仅产物）
- **非 root**：runtime 以 UID 1000 运行
- **HEALTHCHECK**：backend → `/health`，frontend（nginx）→ `/`
- **`.dockerignore`**：排除测试/文档/`.git`/`node_modules`

```bash
# 后端
docker build -f ops/Dockerfile.backend -t aiops/backend:0.1.0 .

# 前端（builder: node:20-alpine → runtime: nginx 托管 dist/）
docker build -f ops/Dockerfile.frontend -t aiops/frontend:0.1.0 .
```

> 前端生产由 **nginx 托管静态文件**，禁止跑 dev server（`ops/nginx.conf`）。

## 2. 环境变量

模板见 [`.env.example`](.env.example)，完整字段定义见 `backend/app/core/config.py`。
**生产环境（`ENVIRONMENT=production/staging/prod`）启动 fail-fast 条件**（`config.py`
的 `_validate_secret_key` / `_validate_required_prod_vars`）：

- `JWT_SECRET` 未设置或仍为默认占位值 `change-me` → 拒绝启动
- `JWT_SECRET` 长度 < 32 字节 → 拒绝启动
- `OPENAI_API_KEY` 为空 → 拒绝启动（LLM 路径无可用 provider）
- `ANTHROPIC_API_KEY` 为空且 `DEFAULT_LLM_PROVIDER=anthropic` → 拒绝启动
- `DEBUG=true` → 拒绝启动（生产禁止明文 traceback，errors.spec.md§5.4）

### 2.1 必需（缺失或弱值则 fail fast 拒绝启动）

> 敏感值必须走 `aiops-secrets` Secret，禁止入 ConfigMap 或镜像（§3.1）。

| 变量 | 说明 | 示例 |
|------|------|------|
| `JWT_SECRET` | JWT 签名密钥（真源，生产 ≥32 字节） | `openssl rand -hex 32` |
| `DATABASE_URL` | PostgreSQL + asyncpg 连接串 | `postgresql+asyncpg://aiops:<pass>@host:5432/aiops` |
| `REDIS_URL` | Redis 连接串 | `redis://host:6379` |
| `OPENAI_API_KEY` | OpenAI Key（LLM + Embedding，生产必填） | `sk-...` |

`SECRET_KEY` 为 `JWT_SECRET` 的兼容别名，优先级低于 `JWT_SECRET`，仅当 `JWT_SECRET`
未显式覆盖时才回退使用。

### 2.2 安全 — JWT / 登录 / 黑名单

| 变量 | 默认 | 说明 |
|------|------|------|
| `JWT_EXPIRE_HOURS` | `24` | access token 过期小时数 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | （空） | 显式设置则覆盖 hours 计算 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | refresh token 过期天数 |
| `TOKEN_BLACKLIST_ENABLED` | `true` | token 黑名单开关，Redis 不可用降级放行 |
| `LOGIN_MAX_FAILURES` | `5` | 连续登录失败 N 次锁定 |
| `LOGIN_LOCKOUT_MINUTES` | `15` | 锁定时长（分钟） |
| `CORS_ORIGINS` | localhost dev 端口 | JSON 数组，生产禁止 `*`（security.spec.md§4） |

### 2.3 数据库 / 缓存 — 连接池

| 变量 | 默认 | 说明 |
|------|------|------|
| `DB_POOL_RECYCLE_SECONDS` | `1800` | pool_recycle，主动回收避开 LB idle 超时 |
| `DB_POOL_TIMEOUT_SECONDS` | `10.0` | 池耗尽时快速失败 |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | PG statement_timeout，慢查询被 PG kill（仅 PG 生效） |

### 2.4 LLM

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEFAULT_LLM_PROVIDER` | `openai` | 默认 provider |
| `DEFAULT_LLM_MODEL` | `gpt-4o-mini` | 默认模型名 |
| `ANTHROPIC_API_KEY` | （空） | Anthropic Key，按需 |
| `LLM_CLIENT_CACHE_MAX_SIZE` | `32` | LLMClient 单例 LRU 上限 |
| `LLM_CLIENT_MAX_CONNECTIONS` | `100` | httpx 连接池上限 |
| `LLM_CLIENT_MAX_KEEPALIVE_CONNECTIONS` | `20` | httpx keepalive 连接数 |
| `LLM_STREAM_CHUNK_TIMEOUT_SECONDS` | `30` | 流式 chunk 间隔超时，判 provider 卡死 |

### 2.5 Agent — 执行 / 调度

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGENT_MAX_TURNS` | `10` | 单次执行最大轮次（长任务可调到 50） |
| `AGENT_EXECUTE_TIMEOUT_SECONDS` | `180` | HTTP 请求级整体超时，超时抛 504 |
| `AGENT_CONTEXT_COMPRESS_TOKENS` | `4000` | context 压缩阈值 token 数 |
| `AGENT_SELF_EVAL_SAMPLES` | `3` | self-eval 采样次数，抑制 judge 噪声 |
| `TASK_REGISTRY_MAX_CONCURRENCY` | `0` | fire-and-forget task 背压上限，0=不限，生产建议 50~200 |
| `AGENT_SCHEDULER_ENABLED` | `false` | autonomous loop worker 开关，生产按需启用 |
| `AGENT_SCHEDULER_INTERVAL_SECONDS` | `60` | worker 扫描间隔 |
| `AGENT_SCHEDULER_CONCURRENCY` | `5` | 并发执行数 |
| `AGENT_SCHEDULER_TIMEOUT_SECONDS` | `300` | 单次执行超时 |
| `AGENT_SCHEDULER_LEASE_SECONDS` | `600` | lease 时长，崩溃恢复阈值 |

### 2.6 Agent — 实验性能力（默认关）

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGENT_MEMORY_ENABLED` | `false` | 记忆层（pgvector 检索注入） |
| `AGENT_MEMORY_TOP_K` | `3` | 记忆检索 top-k |
| `AGENT_QUERY_REWRITE_ENABLED` | `false` | 查询改写 / HyDE |
| `AGENT_QUERY_REWRITE_N_VARIANTS` | `2` | 改写变体数 |
| `AGENT_QUERY_REWRITE_HYDE` | `true` | HyDE 假设文档 |
| `AGENT_COST_ROUTING_ENABLED` | `false` | 成本感知模型路由 |
| `AGENT_COST_TOKEN_BUDGET` | `0` | token 预算窗口，0=不限 |
| `AGENT_COST_BUDGET_WINDOW_SECONDS` | `3600` | 预算窗口时长 |
| `AGENT_COST_BUDGET_REDIS_ENABLED` | `false` | 多副本共享预算（HPA 必须启用） |
| `AGENT_CODE_TOOL_ENABLED` | `false` | code 工具执行（脚枪，默认关） |
| `AGENT_FAILURE_CLUSTERING_ENABLED` | `false` | 失败模式聚类 |
| `AGENT_FAILURE_CLUSTER_DISTANCE_THRESHOLD` | `0.3` | 聚类距离阈值 |
| `AGENT_FAILURE_CLUSTER_DLQ_PATH` | （空） | DLQ 路径，空=不启用 |
| `AGENT_PLANNING_ENABLED` | `false` | Planning 注入 |
| `AGENT_REFLECTION_ENABLED` | `false` | Reflection 产出 |

### 2.7 Online Eval（P0-3 / C5）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ONLINE_EVAL_SAMPLE_RATE` | `0.0` | 生产采样率，0=关闭，0~1 概率 |
| `ONLINE_EVAL_SAMPLE_RATE_BOOST` | `5.0` | 高优先级样本 boost 倍数 |
| `ONLINE_EVAL_PRIORITY_INPUT_LEN_THRESHOLD` | `200` | 输入字符数阈值，超过 priority+1 |

### 2.8 Lifespan / 优雅关闭

| 变量 | 默认 | 说明 |
|------|------|------|
| `LIFESPAN_SHUTDOWN_TIMEOUT_SECONDS` | `20` | shutdown 整体超时，K8s gracePeriod 应 ≥ 此值+10s |

### 2.9 前端

| 变量 | 默认 | 说明 |
|------|------|------|
| `VITE_API_BASE_URL` | `/api/v1` | 前端 API 基址。同源部署（dev Vite proxy / prod nginx 反代）留空即可；前端独立部署到不同 Origin 时显式设为后端绝对地址 |

### 2.10 限流阈值（K8s ConfigMap 引用）

`RATE_LIMIT_DEFAULT_PER_MIN` / `RATE_LIMIT_LLM_PER_MIN` — 见 `ops/k8s/deployment.yaml`
的 `aiops-config` ConfigMap 与 `app/core/ratelimit.py`。

## 3. Kubernetes 部署

manifests 位于 `ops/k8s/`：
- `deployment.yaml`：backend / frontend Deployment + Service、PG StatefulSet、Redis Deployment、ConfigMap、PDB、HPA
- `ingress.yaml`：Ingress + TLS 终止

### 3.1 创建 Secret（密钥注入）

**敏感值必须走 Secret，禁止入 ConfigMap 或镜像**（`deployment.spec.md` §4.1）：

```bash
kubectl create secret generic aiops-secrets \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=database-url="postgresql+asyncpg://aiops:<pg-pass>@aiops-db:5432/aiops" \
  --from-literal=redis-url="redis://aiops-redis:6379" \
  --from-literal=openai-api-key="sk-..." \
  --from-literal=anthropic-api-key="sk-ant-..." \
  --from-literal=db-user="aiops" \
  --from-literal=db-password="<pg-pass>"
```

### 3.2 应用 manifests

```bash
kubectl apply -f ops/k8s/deployment.yaml
kubectl apply -f ops/k8s/ingress.yaml
```

### 3.3 数据库迁移

PG StatefulSet 就绪后，执行 Alembic 迁移（建表 + 种子）：

```bash
kubectl exec -it deploy/aiops-backend -- alembic upgrade head
```

> `init.sql`（挂载到 PG initdb）仅创建 `vector` / `pgcrypto` / `citext` 扩展，
> 业务表与种子数据由 Alembic 管理（`backend/migrations/versions/`）。

### 3.4 高可用拓扑

| 组件 | replicas | HPA | PDB |
|------|----------|-----|-----|
| backend | 2 | CPU>70% / Mem>80%，2-6 | minAvailable: 1 |
| frontend | 2 | CPU>70%，2-4 | minAvailable: 1 |
| PostgreSQL | 1 (StatefulSet + PVC 20Gi) | — | — |
| Redis | 1 | — | — |

## 4. 健康检查

`GET /health` 返回（`deployment.spec.md` §6）：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "checks": {"database": "ok", "redis": "ok"}
}
```

- DB/Redis 均可达 → `ok`；任一不可达 → `degraded`（仍 200）
- **liveness probe**：失败重启 Pod
- **readiness probe**：失败摘除流量（不重启），`degraded` 时摘流

## 5. 可观测性

- **日志**：结构化 JSON（timestamp/level/logger/message/request_id/user_id/latency_ms），`request_id` 贯穿链路
- **指标**：`ops/prometheus/scrape.yml` 采集；`GET /metrics` 暴露 Prometheus 格式（请求数/延迟/错误率/LLM token 与成本）
- **告警**：`ops/prometheus/alerts.yml` — 错误率 >5%、P99 >2s、LLM 成本超阈值

## 6. 升级流程

```bash
# 1. 构建新镜像
docker build -f ops/Dockerfile.backend -t aiops/backend:0.1.1 .

# 2. 推送 + 更新 manifest image tag
# 3. 迁移（先于滚动更新，避免新代码读旧 schema）
kubectl exec -it deploy/aiops-backend -- alembic upgrade head

# 4. 滚动更新
kubectl set image deploy/aiops-backend backend=aiops/backend:0.1.1
kubectl rollout status deploy/aiops-backend

# 回滚
kubectl rollout undo deploy/aiops-backend
# 迁移回滚（仅数据迁移，schema 变更须评估）
kubectl exec -it deploy/aiops-backend -- alembic downgrade -1
```

## 7. 安全检查清单

- [ ] `SECRET_KEY` 已改为随机值，未入 git
- [ ] LLM API Key 仅在 Secret，不触达前端
- [ ] `CORS_ORIGINS` 无 `*`，显式列举允许 Origin
- [ ] 镜像非 root（UID 1000）
- [ ] gitleaks secret 扫描通过（CI `secret-scan` job）
