# Feature: Auth（认证与授权）

> 对齐实现：`app/core/security.py`（bcrypt + JWT HS256）、`app/core/config.py`（`secret_key` / `ACCESS_TOKEN_EXPIRE_MINUTES`）、`app/core/exceptions.py`（`AuthenticationError` / `AuthorizationError`）。
> 领域前缀 `/api/v1/auth`（与 `OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")` 一致）。用户表复用 `backend/init.sql` 中的 `users` 表（不新建表）。

## Goals
- JWT 认证：签发 / 校验 HS256 access token，保护需登录路由
- 用户注册 / 登录：注册写入 `users` 表，登录校验 bcrypt 后签发 token
- token 刷新：通过 refresh token 换取新的 access token（支持 refresh 轮换）
- RBAC 角色控制：`admin` / `user` 两级角色，按角色限制资源操作

## Constraints
- 密码最小长度 8，使用 bcrypt 哈希（`passlib[bcrypt]`，`security.hash_password`）
- access token 过期时间默认 24h，可通过环境变量 `ACCESS_TOKEN_EXPIRE_MINUTES`（默认 `1440`）配置
- token 格式：`Authorization: Bearer <jwt>`；算法 HS256
- 支持 refresh token：默认 7d，可通过 `REFRESH_TOKEN_EXPIRE_DAYS`（默认 `7`）配置；JWT claim `type` 区分 `access` / `refresh`
- 用户表复用 `init.sql` 中的 `users` 表（不新建表）。该表使用 `is_admin BOOLEAN` 表示角色，API 层 `role` 字段由 `is_admin` 派生：`is_admin=true → "admin"`，否则 `"user"`
- email 大小写不敏感（`CITEXT UNIQUE`，依赖 `citext` 扩展），注册 / 登录按小写归一化
- 主键 UUID（`gen_random_uuid()`，依赖 `pgcrypto`）
- 密码不得以明文落库或写入日志；仅 `hashed_password` 入库

## Non-Goals
- OAuth2 第三方登录（Google / GitHub 等）
- 多因素认证（MFA / TOTP）
- SSO / SAML 集成
- 密码找回 / 邮箱验证流程（alpha 版不做）
- 细粒度权限（仅 admin / user 两级）
- token 黑名单 / 主动吊销（alpha 版不做，依赖短过期 + refresh 轮换）

## Success Criteria (Eval)
- [ ] 未认证请求访问受保护路由 → 401，`error="authentication_error"`
- [ ] 登录成功 → 200，返回 `access_token` + `token_type="bearer"`（并含 `refresh_token`、`expires_in`）
- [ ] access token 过期 → 401，`error_code="token_expired"`
- [ ] refresh token 过期或无效 → 401，`error_code="token_expired"` / `"authentication_error"`
- [ ] 密码哈希不可逆：库中仅存 bcrypt hash，`verify_password` 可校验、不可还原
- [ ] 并发登录无竞态：同一账号并发签发 token 不互相破坏
- [ ] 注册 email 重复 → 409，`error_code="conflict"`
- [ ] 非 admin 访问 admin 路由 → 403，`error_code="authorization_error"`

## Data Models
- 持久层：`users` 表（见 `init.sql` / `DATA.spec.md`）。ORM `User` 映射该表，含 `is_admin: bool`。
- API 概念模型：`User（id, email, hashed_password, role, is_active, created_at）`，其中 `role` 由 `is_admin` 派生。
- Pydantic：`UserCreate` / `UserLogin` / `UserOut` / `Token` / `TokenData`（详见 `DATA.spec.md`）。
- JWT claims：access = `{sub, exp, type:"access"}`；refresh = `{sub, exp, type:"refresh"}`。

## API Endpoints
前缀 `/api/v1/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 用户注册（email + username + password） |
| POST | `/auth/token` | 登录获取 token（`OAuth2PasswordRequestForm`：`username`=email, `password`） |
| GET | `/auth/me` | 获取当前用户信息（需 Bearer token） |
| POST | `/auth/refresh` | 刷新 access token（提交 `refresh_token`） |

## RBAC 矩阵
| 资源 / 操作 | admin | user |
|---|---|---|
| 读取所有领域资源（prompts / agents / workflows / knowledge-bases / models / analytics / evals） | 全部 | 只读 |
| 创建 / 更新 / 删除任意资源 | 全部 | 仅管理自己拥有的资源（alpha 期以 `created_by` / `user_id` 判定 owner，缺省视为只读） |
| 管理用户 / 切换 `is_admin` | 全部 | 禁止 |
| 执行 agent / workflow / chat / rag / run-eval | 全部 | 允许（按 owner / 配额限制） |
| `/auth/me`、`/auth/refresh` | 允许 | 允许 |

> alpha 版 RBAC 为粗粒度：`user` = 全量只读 + 操作自己的资源；`admin` = 全量 CRUD。

## Auth Dependencies
- `get_current_user(token=Depends(oauth2_scheme)) -> User`
  - 解析 Bearer token → `verify_token` → 按 `sub`(user_id) 查 `users` 表
  - 未带 / 无效 token → `AuthenticationError`（401，`error_code="authentication_error"`）
  - token 过期 → `AuthenticationError`（401，`error_code="token_expired"`；需在 `verify_token` 中捕获 `jose.ExpiredSignatureError` 单独标记）
  - 用户不存在 / `is_active=false` → `AuthenticationError`（401）
- `get_current_admin(user=Depends(get_current_user)) -> User`
  - 在 `get_current_user` 基础上校验 `is_admin=true`
  - 非 admin → `AuthorizationError`（403，`error_code="authorization_error"`）
- 上述依赖注入到各领域 router 的受保护端点。

## Error Cases
- 未带 / 无效 token → 401 `authentication_error`
- token 过期 → 401 `token_expired`
- 用户不存在 / 已停用 → 401 `authentication_error`
- 注册 email 重复 → 409 `conflict`
- 注册密码 < 8 → 422 `validation_error`
- 非 admin 访问受保护写操作 → 403 `authorization_error`
- 统一错误体：`{error, message, detail}`（与 `main.py` 全局处理器一致）
