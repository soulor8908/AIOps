# AIOps Console 代码审查报告

> **审查标准**: Karpathy Style (Agentic Engineering · Spec-Driven · Minimalism · Understanding-First)
> **审查分支**: feat/spec-driven-auth-and-domain-fixes
> **审查日期**: 2026-07-01
> **审查人**: AI Architect (基于此前设计的 SPEC.md v0.1.0-alpha)

---

## 总体评价

**代码质量: B+** — 整体架构方向正确，DEPENDENCY.md 和异常体系是亮点，但存在多处与 Karpathy 极简主义原则的偏离，需要修正。

| 维度 | 评分 | 说明 |
|------|------|------|
| Spec 契约对齐 | B | 核心流程对齐，但字段扩展未同步回 Spec |
| Minimalism | B+ | 依赖控制良好，但部分文件过大 |
| Understanding-First | A- | 无黑盒依赖，注释质量高 |
| Eval-Driven | C+ | 仅 L1 单元测试，L2-L4 缺失 |
| Flat > Deep | B | 目录结构合理，但个别文件过深 |
| Types as Docs | A- | TypeScript strict 完整，Python 类型良好 |

---

## 一、严重问题 (Must Fix)

### 1. [违反] 文件过大 — security.py 超 50 行限制

**Karpathy 原则**: 函数 < 50 行，超过必须拆分。

**现状**: `backend/app/core/security.py` 约 120+ 行，包含 9 个函数：
- hash_password / verify_password
- _encode
- create_access_token / create_refresh_token
- decode_token / verify_token
- get_current_user / get_current_admin

**问题**: 单一文件职责过重，混合了密码学、JWT 编解码、FastAPI 依赖注入三层逻辑。

**修正方案**:
```
backend/app/core/
  security.py          # 仅保留密码哈希 (< 30 行)
  jwt.py               # JWT 编解码 + 签发 (< 40 行)
  deps.py              # FastAPI 依赖注入 (< 40 行)
```

---

### 2. [违反] 文件过大 — exceptions.py 超 50 行限制

**现状**: 约 100+ 行，包含 9 个异常类定义。

**修正方案**: 拆分为基础异常 + 领域异常，或接受这是类型声明文件的例外。

**建议**: 如果坚持严格 50 行，拆分为：
```
backend/app/core/
  exceptions.py        # AppError 基类 + 通用异常 (< 30 行)
  errors/              # 领域异常 (按 HTTP 状态码分组)
    auth.py            # AuthenticationError, TokenExpiredError
    resource.py        # NotFoundError, ConflictError
    system.py          # RateLimitError, LLMError
```

---

### 3. [违反] Eval 体系不完整 — 仅 L1，L2-L4 缺失

**Karpathy 原则**: 每个功能必须有可自动验证的 eval，L1-L3 必须 100% 通过，L4 > 0.85。

**现状**:
- ✅ L1: `test_prompts.py` 有 6 个单元测试，使用 SQLite in-memory
- ❌ L2: `schemathesis` 在 pyproject.toml 中声明，但**无实际测试文件**
- ❌ L3: 无 Playwright E2E 测试
- ❌ L4: 无 LLM-as-judge 测试

**修正方案**:
```
backend/app/domains/prompts/tests/
  test_prompts.py        # L1 单元测试 (已有)
  test_api_contract.py   # L2 schemathesis 契约测试 (新增)
  eval_prompts.py        # L4 LLM-as-judge (新增)
frontend/e2e/
  prompts.spec.ts        # L3 Playwright E2E (新增)
```

---

### 4. [违反] Spec 未同步 — 代码扩展字段未回写 SPEC.md

**Karpathy 原则**: 每次架构变更必须先更新 SPEC.md，再更新 eval，再更新代码。

**发现的不一致**:

| 字段 | 原始 SPEC | 实际代码 | 状态 |
|------|-----------|----------|------|
| PromptVersion.change_note | 未定义 | 已添加 | ❌ Spec 未更新 |
| Prompt.is_active | 未定义 | 已添加 | ❌ Spec 未更新 |
| User.username | 未定义 | 已添加 | ❌ Spec 未更新 |
| User.full_name | 未定义 | 已添加 | ❌ Spec 未更新 |
| User.is_admin | 未定义 (role) | 已添加 | ❌ Spec 未更新 |
| Token.refresh_token | 未定义 | 双 token | ❌ Spec 未更新 |
| User.is_active | 未定义 | 已添加 | ❌ Spec 未更新 |

**修正方案**: 创建 `specs/changes/` 目录，记录所有 Spec 变更。

---

## 二、中等问题 (Should Fix)

### 5. [偏离] service.py 函数签名 — 注入 session 参数

**原始 Spec 设计**:
```python
async def create_prompt(data: PromptCreate) -> Prompt:
    async with async_session() as session:
        ...
```

**实际代码**:
```python
async def create_prompt(session: AsyncSession, data: PromptCreate) -> Prompt:
    ...
```

**分析**: 这不是错误，而是依赖注入风格 vs 自管理 session 风格的选择。

**建议**: 在 `backend/SPEC.md` 中增加一节 Session 管理策略，说明为何采用注入式。

---

### 6. [缺失] 前端 OpenAPI 类型生成未执行

**原始 Spec 要求**:
```bash
npx openapi-typescript specs/openapi.yaml -o frontend/src/shared/api/types.ts
```

**现状**: 无 `frontend/src/shared/api/types.ts` 文件，前端类型似乎手写或缺失。

**影响**: 前后端类型不一致风险，违背 Types as Docs 原则。

**修正方案**:
1. 确保 `specs/openapi.yaml` 与代码同步
2. 执行类型生成命令
3. 将生成命令加入 `frontend/package.json` scripts

---

### 7. [偏离] pyproject.toml 使用 setuptools 而非 Poetry

**原始 Spec 设计**: Poetry 配置

**实际代码**: setuptools 配置

**分析**: setuptools 是 Python 标准，这不是功能问题，但说明 Spec 与实际决策存在偏差。

**建议**: 更新 `backend/SPEC.md` 或 `DEPENDENCY.md`，说明为何选择 setuptools 而非 Poetry。

---

### 8. [缺失] 前端缺少 views/ 页面层

**原始 Spec 目录结构**:
```
frontend/src/views/        # 页面（薄层，只组装 domain 组件）
```

**现状**: 无 `views/` 目录，可能直接由 `domains/*/components/` 充当页面。

**分析**: 如果项目规模小（< 10 个页面），可以省略 views/ 层。但需在 SPEC.md 中说明。

---

### 9. [缺失] Docker / K8s 配置未实现

**原始 Spec 包含**:
- `docker-compose.yml`
- `ops/Dockerfile.backend`
- `ops/Dockerfile.frontend`
- `ops/k8s/deployment.yaml`

**现状**: 未在仓库中发现这些文件。

**影响**: 无法一键启动开发环境，违背 1 个迭代内从 0 到 MVP 的交付效率原则。

**优先级**: P1（影响开发 onboarding）

---

## 三、轻微问题 (Nice to Fix)

### 10. [风格] 注释中中文比例偏高

**Karpathy 原则**: 注释只解释 为什么，不解释 做什么（代码自解释）。类型即文档，零注释冗余。

**现状示例** (`security.py`):
```python
"""安全模块 - JWT + OAuth2 + 密码哈希 + RBAC 依赖（极简实现）。

覆盖 `auth/SPEC.md` Auth Dependencies 与 `specs/security.spec.md` 2/3：
- access/refresh token 双 token（JWT claim type 区分）
- verify_token 单独捕获 ExpiredSignatureError 抛 TokenExpiredError
- get_current_user / get_current_admin FastAPI 依赖注入
"""
```

**分析**: 注释质量很高，解释了设计决策（为什么），但行数偏多。

**建议**: 保留核心设计决策注释，删除对 SPEC 条款的引用（这些应在 commit message 中体现）。

---

### 11. [风格] `__all__` 导出列表冗长

**现状** (`security.py` 末尾):
```python
__all__ = [
    "ALGORITHM",
    "TOKEN_TYPE_ACCESS",
    ...  # 共 13 项
]
```

**分析**: 如果文件按建议拆分，每个小模块自然导出全部内容，`__all__` 可省略。

---

### 12. [缺失] `agents.md` 未在仓库根目录完整呈现

**原始 Spec 要求**: `agents.md` 是 LLM 的上下文程序，位于仓库根目录。

**现状**: 从 README 引用看存在，但未在 raw 内容中完整加载。

**建议**: 确保 `agents.md` 完整且可被 AI Agent 直接读取。

---

## 四、优秀实践 (Keep Doing)

### ✅ DEPENDENCY.md 质量极高

- 每个依赖都有版本、理由、替代方案评估
- 明确拒绝 LangChain / Celery / alembic / sqlmodel / fastapi-users
- 依赖更新策略清晰

**Karpathy 评分**: A+

### ✅ 异常体系设计优秀

- `AppError` 基类统一所有异常
- 每个异常携带 status_code + error_code + message
- `to_response()` 方法避免返回 null
- `TokenExpiredError` 继承 `AuthenticationError` 的设计合理

**Karpathy 评分**: A

### ✅ 测试策略合理

- SQLite in-memory 避免 PG 依赖
- pytest-asyncio 适配 async service
- 覆盖率门槛 80% 已配置

**Karpathy 评分**: B+ (缺少 L2-L4)

### ✅ 前端 client.ts 极简实现

- 基于 fetch，零 axios 依赖
- 401 自动清除 token
- 204 状态正确处理
- upload helper 独立处理 multipart

**Karpathy 评分**: A

### ✅ TypeScript Strict 模式完整

- noUnusedLocals: true
- noUnusedParameters: true
- noImplicitReturns: true
- exactOptionalPropertyTypes: false (合理)

**Karpathy 评分**: A

### ✅ 工具函数纯函数化

`frontend/src/shared/utils/index.ts`:
- 无副作用
- 无外部依赖
- 输入输出明确
- `cn()` 函数替代 clsx (约 5 行实现)

**Karpathy 评分**: A+

---

## 五、修正优先级清单

| 优先级 | 问题 | 工作量 | 影响 |
|--------|------|--------|------|
| P0 | 拆分 security.py 为 3 个文件 | 30 min | 代码可读性 |
| P0 | 补充 L2-L4 eval 测试 | 4-8 h | 质量保证 |
| P0 | 同步 Spec 与实际代码字段 | 1 h | 契约一致性 |
| P1 | 添加 docker-compose.yml | 1 h | 开发效率 |
| P1 | 生成前端 OpenAPI 类型 | 30 min | 类型安全 |
| P2 | 精简 security.py 注释 | 15 min | 风格一致性 |
| P2 | 确认 agents.md 完整性 | 15 min | Agent 协作 |

---

## 六、Karpathy 风格合规检查表

| 原则 | 合规 | 说明 |
|------|------|------|
| **Spec 即契约** | 部分 | 核心流程对齐，但字段扩展未同步 |
| **Minimalism** | 部分 | 依赖控制优秀，但 security.py 过大 |
| **Understanding-First** | 合规 | 无黑盒依赖，自研 LLM 客户端 |
| **Eval-Driven** | 不合规 | 仅 L1，L2-L4 缺失 |
| **Context as Code** | 部分 | agents.md 存在但需确认完整 |
| **Flat > Deep** | 合规 | 目录结构扁平，无深层嵌套 |
| **Types as Docs** | 合规 | TypeScript strict + Python 类型完善 |

---

> **结论**: 代码骨架健康，架构方向正确，但需要在 **Eval 体系完整性**、**Spec 同步**、**文件粒度控制** 三个维度上加强，以完全符合 Karpathy 风格要求。
