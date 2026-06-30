# AIOps Console - Agent 工作流配置

> Karpathy 风格：agents.md 是 LLM 的上下文程序。
> 每个 Agent 必须理解此文件，才能参与本项目的开发。

## 1. 项目元信息

- **名称**：AIOps Console
- **语言**：Python 3.12 (backend) / TypeScript 5.6 (frontend)
- **框架**：FastAPI · Vue 3 · SQLAlchemy 2.0 · Pinia 3
- **风格**：Minimalism · Flat structure · Understanding-first

## 2. 开发工作流（强制）

```
1. 阅读相关 SPEC.md
2. 根据 SPEC 编写 eval（测试先于代码）
3. 实现代码，引用 SPEC 条款编号
4. 运行 eval（L1-L3 必须 100% 通过，L4 > 0.85）
5. 提交 PR（diff < 200 行，大 feature 拆分）
```

## 3. 代码风格规则

### Python
- 使用函数式代码，纯函数优先
- 类型注解必须完整，禁止 Any（除非确实任意）
- 函数长度 < 50 行，超过必须拆分
- 注释只解释"为什么"，不解释"做什么"，代码自解释
- 异常处理：显式抛出，不吞异常

### TypeScript / Vue
- 严格模式开启，禁止 any
- 使用 script setup + 组合式 API
- 组件 < 150 行，超过拆分
- API 层使用生成的类型，禁止手写接口定义

## 4. 依赖决策树

```
需要这个功能
  能用50行以内实现 → 自己写
  不能 → 它有多少 transitive dependencies
       <5 → 可以考虑
       >20 → 红旗，找替代或 yoink 核心代码
       必须引入 → 在 DEPENDENCY.md 中说明理由
```

## 5. 禁止模式

- 禁止引入 LangChain/LangGraph，自研 LLM 客户端
- 禁止引入 ORM 之上的 DAO 层，直接用 SQLAlchemy
- 禁止手写 API 类型，从 OpenAPI 生成
- 禁止深层目录嵌套（max 3 层）
- Store 通过 api.ts 模块调用 API，禁止直接使用 fetch；api.ts 封装 HTTP 细节（基于 shared/api/client.ts），store 管理 state + 编排（与 frontend/SPEC.md§4.4 对齐，详见 §9）

## 6. Eval 规范

每个功能必须有：
- **L1** pytest 单元测试，覆盖率 > 80%
- **L2** schemathesis 契约测试
- **L3** Playwright E2E 测试（关键路径）
- **L4** LLM-as-judge 语义质量

## 7. Commit Message 格式

```
<domain> <action> <target>

- 引用SPEC <spec-file>#<clause>
- Eval <eval-file> 通过
```

Example:
```
prompts add version rollback

- 引用SPEC SPEC.md#5.1
- Eval eval_prompts.py 通过 L1-L4
```

## 8. 多 Agent 协作模式

当使用多个 Agent 并行开发时：
- **Agent A** 负责 API 层（router + models）
- **Agent B** 负责业务逻辑（service）
- **Agent C** 负责 eval 和测试
- **人工** 负责 review diff 和合并

每个 Agent 的上下文必须包含：
1. 本 agents.md
2. 相关 SPEC.md
3. 已生成的 OpenAPI spec（确保类型一致）

## 9. 认证工作流规则

> 详见 `specs/security.spec.md`、`specs/errors.spec.md`。本节为 Agent 开发时必须遵守的认证编排要点。

- **认证机制**：JWT Bearer token（`Authorization: Bearer <token>`），过期时间可配置（默认 24h）。
- **Token 生命周期**：
  - 登录成功签发 token；过期返回 `401 token_expired`（统一错误格式见 `errors.spec.md`§4）。
  - 登出采用客户端丢弃 token + 服务端 Redis 黑名单（TTL = 剩余有效期）双重策略。
- **授权（RBAC）**：角色 `admin` / `user`；权限检查在路由层通过依赖注入完成，禁止散落到 service 层。
  - 资源所有权校验必须显式（如 `resource.owner_id == current_user.id`）。
  - 默认拒绝：未声明权限的端点视为需要 `admin`。
- **前端编排**：
  - `client.ts` 在请求头注入 `Authorization`；收到 `401 token_expired` 时由 store 触发跳转登录，禁止在组件层解析状态码。
  - token 存储于内存/Pinia store，敏感操作前校验有效性；禁止将 token 写入可被 XSS 读取的位置。
- **密钥隔离**：`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` 仅服务端持有，前端调用 LLM 一律经后端代理端点，禁止凭据触达前端。

## 10. 测试覆盖率门槛配置

> 详见 `specs/testing.spec.md`。本节为 CI 门禁与本地配置要点。

- **四层金字塔**：L1 pytest 单元 / L2 schemathesis 契约 / L3 Playwright E2E / L4 LLM-as-Judge。
- **L1 覆盖率门槛**：`pytest --cov=app --cov-report=term-missing --cov-fail-under=80`，低于 80% CI 直接失败。
  - 前端单元测试（Vitest）同样 80% 覆盖率门槛。
  - 禁止滥用 `# pragma: no cover`，每处须注释理由。
- **CI 门禁**：
  - L1–L3 必须 100% 通过，PR 不通过不可合并。
  - L4 得分 > 0.85，未达不阻断合并但须人工评审并记录。
  - 流程：lint → L1 → L2 → L3 → L4 → coverage report。
- **测试数据**：每用例独立 fixture + factory pattern，禁止共享状态与隐式执行顺序依赖；数据库测试在事务内运行、结束回滚。
- **L3 范围**：必须覆盖"登录 → 创建 Prompt → 版本管理 → 回滚"，且对 `vite build` 产物（`dist/`）运行，而非 dev server。

## 11. 横切关注点 Spec 引用

以下横切关注点 Spec 为全栈强制契约，开发前必读：

| 关注点 | Spec 文件 | 要点 |
|--------|-----------|------|
| 错误处理 | `specs/errors.spec.md` | 统一 `{error, message, detail}` 格式、HTTP 状态码、AppError 体系、500 兜底、前端 ApiError |
| 安全 | `specs/security.spec.md` | JWT/RBAC、CORS、Redis 限流、bcrypt、文件上传、API Key 隔离、Secret 管理 |
| 测试 | `specs/testing.spec.md` | 四层金字塔、覆盖率 80% 门槛、CI 门禁、factory pattern |
| 数据库迁移 | `specs/migration.spec.md` | ORM 单一真源、Alembic 流程、init.sql 边界、漂移修复清单 |
| 部署 | `specs/deployment.spec.md` | 多阶段镜像、非 root、nginx 托管、K8s 副本/HPA/PDB、健康检查 |
| 可观测性 | `specs/observability.spec.md` | JSON 结构化日志、request_id 追踪、指标、告警、前端监控 |

每次涉及横切关注点的变更，必须先更新对应 `specs/*.spec.md`，再更新 eval，再更新代码。
