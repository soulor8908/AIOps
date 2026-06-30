# 横切关注点 Spec — 部署（Deployment）

> Version: v0.1.0-alpha | Date: 2026-06-30
> Scope: Docker 镜像标准、前端生产部署、K8s 部署、环境变量、健康检查
> 关联: SPEC.md#6-部署、security.spec.md（Secret）、observability.spec.md（健康检查）

---

## 1. 目标

为 AIOps Console 提供可重复、安全、最小化的部署标准：
- 镜像小、非 root、自带健康检查。
- 前端生产只跑静态文件，禁止 dev server。
- K8s 部署高可用、可探活、配置与密钥分离。

## 2. Docker 镜像标准

所有镜像必须满足以下基线：

### 2.1 多阶段构建
- 采用 **builder + runtime** 两阶段：
  - builder 阶段：安装依赖、编译、构建产物。
  - runtime 阶段：仅拷贝产物 + 运行时依赖，丢弃编译工具链。
- 目的：最终镜像最小化，减小攻击面与拉取时间。

### 2.2 非 root 用户
- runtime 阶段以 **UID 1000** 非 root 用户运行。
- Dockerfile 内 `RUN useradd -u 1000 -m app && USER app`（或等价）。
- 禁止以 root 启动容器进程。

### 2.3 HEALTHCHECK
- 每个镜像必须包含 `HEALTHCHECK` 指令，指向健康端点（见 §6）。
- backend：`HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1`
- frontend（nginx）：`HEALTHCHECK CMD wget -q --spider http://localhost/ || exit 1`

### 2.4 .dockerignore
- `.dockerignore` 必须排除：测试目录、文档（`*.md`）、`.git`、`node_modules`、`__pycache__`、`.venv`、构建缓存。
- 目的：减小构建上下文，加速构建，避免敏感文件入镜像。

### 2.5 基础镜像
| 组件 | 基础镜像 | 说明 |
|------|----------|------|
| backend | `python:3.12-slim` | 与运行时 Python 版本一致，slim 减小体积 |
| frontend | `node:20-alpine`（builder）→ `nginx`（runtime） | builder 用 node 构建，runtime 用 nginx 托管静态文件 |

## 3. 前端生产部署

- 构建：`vite build` 产出 `dist/`。
- 托管：**nginx 托管 `dist/` 静态文件**。
- **禁止生产环境跑 dev server**（`vite dev`）：dev server 含 HMR、源码映射、未压缩资源，不可用于生产。
- nginx 配置：
  - SPA history 模式 fallback（`try_files $uri $uri/ /index.html`）。
  - `/api` 反向代理到 backend service。
  - 静态资源启用 gzip/brotli 与缓存头。

## 4. K8s 部署

### 4.1 配置与密钥分离
- **Secret** 管理敏感配置：`JWT_SECRET`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、数据库密码、Redis 密码。
- **ConfigMap** 管理非敏感配置：`JWT_EXPIRE_HOURS`、`LOG_LEVEL`、CORS origins、限流阈值。
- 禁止将敏感值放入 ConfigMap。

### 4.2 backend Deployment
- **2+ replicas**：最少两个副本，保证滚动更新与单点容错。
- **HPA**（Horizontal Pod Autoscaler）：基于 CPU/内存或自定义指标自动扩缩。
- **PDB**（PodDisruptionBudget）：`minAvailable: 1`，保证 voluntary disruption 期间至少一个可用。
- liveness probe + readiness probe（见 §6）。
- 资源 `requests` / `limits` 必填。

### 4.3 frontend Deployment
- **2+ replicas**：最少两个副本。
- **PDB**：`minAvailable: 1`。
- liveness probe + readiness probe。
- 资源 `requests` / `limits` 必填（前端资源占用低，但仍须设定上限）。

### 4.4 Ingress + TLS
- 通过 Ingress 暴露服务，TLS 终止于 Ingress（证书由 cert-manager 等自动管理）。
- HTTP → HTTPS 强制跳转。
- 前端走根路径 `/`，backend 走 `/api` 前缀。

## 5. 环境变量管理

- `.env.example`：作为模板入库，仅含占位符与注释，不含真实值。
- `.env`：本地开发用，**不入 git**（`.gitignore` 已排除）。
- 生产环境：使用 **K8s Secret / ConfigMap** 注入，禁止镜像内烘焙环境变量。
- 启动时校验必需变量存在，缺失则拒绝启动（fail fast），避免静默错误行为。

## 6. 健康检查

- 健康端点：`GET /health`。
- 响应：`{ "status": "ok", "version": "<app_version>" }`。
- status 仅在依赖（数据库、Redis）可达时为 `ok`，否则 `degraded`（仍 200，但 readiness 可据此摘流）。
- liveness probe：`/health`，失败重启 Pod。
- readiness probe：`/health`，失败摘除流量但不重启。

## 7. 验收清单

- [ ] backend/frontend 镜像均为多阶段构建、非 root（UID 1000）、含 HEALTHCHECK。
- [ ] `.dockerignore` 排除测试/文档/git/node_modules。
- [ ] 前端生产由 nginx 托管 `dist/`，无 dev server。
- [ ] K8s：backend/frontend 均 2+ replicas、HPA、PDB、probe、resources。
- [ ] Secret 与 ConfigMap 分离，无敏感值入 ConfigMap。
- [ ] `.env` 不入 git，`.env.example` 为模板。
- [ ] `/health` 返回 status + version，liveness/readiness 已配置。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次部署拓扑变更必须先更新本文件，再更新 ops/ manifests，再上线。
