# 横切关注点 Spec — 安全（Security）

> Version: v0.1.0 | Date: 2026-07-03
> Scope: 认证、授权、CORS、限流、密码、文件上传、密钥管理
> 关联: SPEC.md#安全、errors.spec.md（401/403/429）、deployment.spec.md（Secret 注入）

---

## 1. 目标

为 AIOps Console 建立最小化且可审计的安全基线：
- 认证基于 JWT，授权基于 RBAC，规则可配置。
- 生产环境不留任何"开发期方便"的开放配置（通配 CORS、硬编码密钥）。
- LLM 凭据与用户密钥绝不触达前端。

## 2. 认证（Authentication）

### 2.1 机制
- 采用 **JWT Bearer token**（RFC 6750 / RFC 7519）。
- 传输：`Authorization: Bearer <token>` 请求头。
- 算法：`HS256`（单租户企业部署足够；如需非对称签名后续切换 RS256）。

### 2.2 Token 生命周期
- 过期时间可配置，环境变量 `JWT_EXPIRE_HOURS`，默认 **24h**。
- Token 过期返回 `401 token_expired`（见 `errors.spec.md`§4）。
- 登出采用客户端丢弃 token + 服务端黑名单（Redis，TTL = 剩余有效期）双重策略。

### 2.3 签名密钥
- `JWT_SECRET` 通过环境变量注入，禁止硬编码。
- 最小长度 32 字节，生产环境从 K8s Secret 读取（见 `deployment.spec.md`）。

## 3. 授权（Authorization）

### 3.1 RBAC 模型
- 角色：`admin`、`user`（最小集合，按需扩展）。
- `admin`：可访问所有端点，含用户管理、模型配置、密钥配置等管理类操作。
- `user`：受限访问，仅可操作自有资源；管理类端点禁止访问，返回 `403 permission_denied`。

### 3.2 实现规则
- 权限检查在路由层通过依赖注入完成，禁止在 service 层散落判断。
- 资源所有权校验（如"只能改自己的 Prompt"）必须显式校验 `resource.owner_id == current_user.id`。
- 默认拒绝：未显式声明权限的端点视为需要 `admin`。

## 4. CORS

- 生产环境**必须**指定明确的 `allow_origins`（如 `["https://console.aiops.example.com"]`）。
- **禁止** `allow_origins=["*"]` 与 `allow_credentials=True` 同时出现（浏览器规范亦不允许，且是高危误配）。
- `allow_methods`、`allow_headers` 显式列举所需项，不使用通配。
- 开发环境可放宽到 `["http://localhost:5173"]`，但配置须与生产分离。

## 5. 限流（Rate Limiting）

### 5.1 策略
- 基于 **Redis 的滑动窗口限流**（sliding window）。
- 维度：per user（已认证）/ per IP（未认证，如登录端点）。
- 默认配额：**100 req/min per user**。
- LLM 端点（`/api/llm/*`、`/api/chat/*` 等调用模型推理的端点）：**20 req/min**，因成本与资源敏感。

### 5.2 响应
- 超限返回 `429 rate_limited`（见 `errors.spec.md`§4）。
- 响应头附加 `X-RateLimit-Limit`、`X-RateLimit-Remaining`、`X-RateLimit-Reset`。
- 限流实现不得阻塞事件循环（Redis 异步操作）。

## 6. 密码安全

- 哈希算法：**bcrypt**（cost factor ≥ 12）。
- 最小长度：**8** 字符。
- 禁止明文存储、禁止 MD5/SHA1/无盐 SHA256。
- 登录失败计数：连续失败 5 次锁定账户 15 分钟（Redis 计数）。
- 密码重置令牌一次性使用，TTL ≤ 30 分钟。

## 7. 文件上传安全

适用于 Knowledge Base 文档上传等端点。

### 7.1 校验顺序
1. **先检查 `Content-Length`**，超限直接拒绝，再读取 body（防止大文件耗尽内存/带宽）。
2. 读取后校验 `content-type`。
3. 校验文件大小（与 header 一致，防伪造）。

### 7.2 content-type 白名单
仅允许：
- `text/plain`
- `text/markdown`
- `application/pdf`

禁止 `application/octet-stream`、可执行类、脚本类 MIME。

### 7.3 大小限制
- 单文件最大 **50MB**。
- 超限返回 `422 validation_error`，`detail` 指明 `max_size` 与实际大小。

### 7.4 存储安全
- 上传文件存储路径不得可执行。
- 文件名必须重命名（UUID），禁止保留用户原始文件名拼接路径（防目录穿越）。

## 8. API Key 保护

- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 等 LLM 凭据**仅存服务端**。
- **禁止**以任何形式暴露给前端：不写入响应体、不写入前端可访问的配置接口、不通过 `import.meta.env` 注入。
- 前端调用 LLM 能力一律经后端代理端点，后端注入凭据后转发。
- 后端日志禁止打印完整 API Key（可脱敏显示前 4 位 + `***`）。

## 9. Secret 管理

- **生产环境**：通过环境变量 / K8s Secret 注入，禁止硬编码、禁止写入 git。
- **本地开发**：`.env` 文件，已加入 `.gitignore`；`.env.example` 作为模板入库，仅含占位符（`OPENAI_API_KEY=changeme`）。
- 代码中禁止出现字面量密钥（CI 通过 secret 扫描拦截，如 `gitleaks`）。
- 轮换：生产密钥需支持无停机轮换，配置热加载或滚动重启生效。

## 10. 验收清单

- [x] JWT 过期可配置，默认 24h，签名密钥来自环境变量。
- [x] RBAC 依赖注入实现，admin/user 权限边界测试通过。
- [x] 生产 CORS 无 `*` + credentials 组合。
- [x] 限流：默认 100/min，LLM 端点 20/min，超限返回 429 + 限流头。
- [x] 密码 bcrypt 哈希，最小 8 位。
- [x] 文件上传：先检 Content-Length，白名单 MIME，50MB 上限。
- [x] CI secret 扫描通过，无硬编码密钥。
- [x] LLM API Key 不出现在任何前端可达路径。

### 10.1 落地记录

- **Phase 3 batch 1**（分支 `feat/phase3-security-baseline`，合并到 main）：
  - §5 限流：Redis ZSET 滑动窗口中间件（`app/core/rate_limit.py`），默认 100/min、
    LLM 端点 20/min（独立 key 计数，互不挤占），429 + `X-RateLimit-*` 头，Redis 不可用降级放行。
    `fakeredis` 覆盖 8 测试（`tests/test_core_rate_limit.py`）。
  - §9 / §10 secret 扫描：CI 新增 `secret-scan` job（`gitleaks-action@v2`，阻断合并），
    `.gitleaks.toml` 继承内置规则 + 测试/模板 allowlist。
  - §4 CORS：`config.py` 新增 `_validate_cors` model_validator，禁止 `cors_origins` 含 `*`
    （fail-fast，与 `allow_credentials=True` 互斥），4 测试覆盖。
  - §7 文件上传：knowledge router Content-Length 预检（超 51MB 直接拒绝）、MIME 白名单
    （`text/plain` / `text/markdown` / `application/pdf`）、UUID 重命名防目录穿越，3 安全测试。
  - §2 JWT / §6 密码 / §8 API Key：既存实现满足（bcrypt 哈希、`min_length=8`、
    LLM 凭据仅服务端注入转发、`frontend/src` 无 key 引用，已校验）。
- **Phase 3 batch 2**（分支 `feat/phase3-rbac`，合并到 main）：
  - §3.2 RBAC — 6 个 domain router（agents / prompts / evals / models / analytics /
    knowledge）全部挂载 `get_current_user` / `get_current_admin` 依赖；读取类端点
    → `user`，创建/更新/删除类端点 → `admin`，执行类端点 → `user`（auth/SPEC.md §60-62）。
  - 测试 fixture 三层覆盖：`client`（默认 admin，既有功能测试零改动）、`anon_client`
    （关闭认证覆盖，401 边界）、`user_client`（普通用户，403 边界）。
  - `tests/test_rbac_boundary.py`：14 个 401 参数化（无 token）+ 12 个 403（user 访问
    admin 端点）+ 8 个 200 正向校验（user 访问 user 级端点），共 34 测试全绿。
  - §10 验收清单 8/8 全部勾选，Phase 3 安全基线落地完成。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次安全相关变更必须先更新本文件，再更新 eval，再更新代码。
