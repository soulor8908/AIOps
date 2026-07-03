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
docker build -f ops/Dockerfile.backend -t aiops/backend:0.1.0 backend/

# 前端（builder: node:20-alpine → runtime: nginx 托管 dist/）
docker build -f ops/Dockerfile.frontend -t aiops/frontend:0.1.0 frontend/
```

> 前端生产由 **nginx 托管静态文件**，禁止跑 dev server（`ops/nginx.conf`）。

## 2. 环境变量

模板见 [`.env.example`](.env.example)。变量分类：

### 必需（缺失则 fail fast 拒绝启动）

| 变量 | 说明 | 示例 |
|------|------|------|
| `SECRET_KEY` | JWT 签名密钥（生产必须改） | 随机 32+ 字节 |
| `DATABASE_URL` | PostgreSQL + asyncpg 连接串 | `postgresql+asyncpg://user:pass@host:5432/aiops` |
| `REDIS_URL` | Redis 连接串 | `redis://host:6379` |

### LLM（按需）

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | OpenAI（LLM + Embedding）；空则 embedder 回退零向量 |
| `ANTHROPIC_API_KEY` | Anthropic（Claude） |
| `DEFAULT_LLM_PROVIDER` | `openai` / `anthropic` |
| `DEFAULT_LLM_MODEL` | 默认模型名 |

### 非敏感配置（ConfigMap）

`ENVIRONMENT` / `LOG_LEVEL` / `JWT_EXPIRE_HOURS` / `CORS_ORIGINS` / 限流阈值 / `VITE_API_BASE_URL` — 见 `ops/k8s/deployment.yaml` 的 `aiops-config` ConfigMap。

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
docker build -f ops/Dockerfile.backend -t aiops/backend:0.1.1 backend/

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
