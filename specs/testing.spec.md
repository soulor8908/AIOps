# 横切关注点 Spec — 测试（Testing）

> Version: v0.1.0 | Date: 2026-07-03
> Scope: 四层测试金字塔、CI 门禁、覆盖率门槛、测试数据策略
> 关联: SPEC.md#测试、agents.md#6-Eval规范、errors.spec.md（错误码契约测试）

---

## 1. 目标

建立 AIOps Console 的测试金字塔，确保：
- 每一层职责清晰、工具固定、门槛可执行。
- CI 强制门禁，PR 不达门槛不可合并。
- 测试独立可重复，不依赖共享状态或执行顺序。

## 2. 测试金字塔（四层）

```
            ▲
           / \
          / L4 \   LLM-as-Judge（语义质量，少量，> 0.85）
         /───────\
        /    L3    \  E2E（关键路径，少量）
       /────────────\
      /      L2      \  契约测试（所有端点）
     /────────────────\
    /        L1         \  单元测试（多数，≥ 80%）
   /──────────────────────\
```

| 层级 | 名称 | 工具 | 范围 | 门槛 |
|------|------|------|------|------|
| L1 | 单元测试 | pytest | service 层纯函数 + 边界 | 覆盖率 ≥ 80%，100% 通过 |
| L2 | 契约测试 | schemathesis | 所有 OpenAPI 端点的状态码/schema | 覆盖所有端点，100% 通过 |
| L3 | E2E 测试 | Playwright | 关键用户路径 | 100% 通过 |
| L4 | LLM-as-Judge | 自研 eval | AI 输出质量 | 得分 > 0.85 |

## 3. L1 单元测试（pytest）

### 3.1 范围
- 重点覆盖 **service 层纯函数**：业务逻辑、校验、转换、路由策略。
- ORM 查询层用 SQLite in-memory 跑（避免依赖外部 PostgreSQL，提速）。
- 需 pgvector 的查询测试单独标记，在集成环境用真实 PostgreSQL 运行。

### 3.2 覆盖率门槛
- 覆盖率工具：**pytest-cov**。
- 命令：`pytest --cov=app --cov-report=term-missing --cov-fail-under=80`。
- 低于 80% 直接 CI 失败，禁止 `# pragma: no cover` 滥用（每处须注释理由）。
- SQLite in-memory 通过 `conftest.py` fixture 提供会话，测试结束回滚。

### 3.3 编写规则
- 一个测试只验证一个行为，命名 `test_<行为>_<条件>`。
- 异常路径必须覆盖（如 `not_found`、`validation_error`）。
- 禁止真实网络/真实 LLM 调用，使用 stub/fake。

## 4. L2 契约测试（schemathesis）

### 4.1 输入与范围
- 从 `specs/openapi.yaml` 运行，覆盖**所有**端点。
- 校验项：返回状态码是否在 OpenAPI 声明范围内、响应 schema 是否匹配。

### 4.2 规则
- 契约测试失败 = 实现与 OpenAPI 不一致，必须修复其中一方并同步。
- 错误响应统一格式（`errors.spec.md`§2）须作为 schema 一部分被校验。
- CI 中 `schemathesis run` 失败即阻断合并。

## 5. L3 E2E 测试（Playwright）

### 5.1 关键用户路径
必须覆盖的端到端路径：
1. 登录 → 创建 Prompt → 版本管理（新增版本）→ 回滚到上一版本。
2. （其余领域按迭代补充，但上述 Prompt 路径为最小必选项。）

### 5.2 规则
- 对**前端构建产物**（`vite build` 后的 `dist/`）测试，而非 dev server，确保生产构建可用。
- 测试数据通过 API 或 fixture 在测试前准备、测试后清理。
- E2E 不得跳过认证，必须走真实登录流程。

## 6. L4 LLM-as-Judge（自研 eval）

### 6.1 范围
- 评估 AI 输出质量：Prompt 渲染结果、RAG 检索相关性、Agent 输出合理性。
- 自研轻量 eval 框架，禁止引入重型 eval 平台。

### 6.2 门槛
- 得分 **> 0.85**（满分 1.0）。
- 低于门槛：不阻断合并（语义质量需人工判断），但会在 PR 报告中标红，触发评审。

## 7. CI 强制门禁

| 层级 | 门禁 | 阻断合并 |
|------|------|----------|
| L1 | 100% 通过 + 覆盖率 ≥ 80% | 是 |
| L2 | 100% 通过 | 是 |
| L3 | 100% 通过 | 是 |
| L4 | 得分 > 0.85 | 否（标红，人工评审） |

- **L1–L3 必须 100% 通过，PR 不通过不可合并。**
- L4 未达 0.85 不阻断合并但必须有人工评审记录。
- CI 流程：lint → L1 → L2 → L3 → L4 → coverage report。

## 8. 测试数据策略

- 每个测试使用**独立 fixture**，不依赖共享全局状态。
- 使用 **factory pattern** 创建测试数据（如 `UserFactory.build()`、`PromptFactory.build()`），禁止直接构造散落字面量。
- 数据库测试每个用例在事务内运行、结束回滚，保证隔离。
- 禁止测试间隐式依赖顺序（不依赖"前一个测试创建了数据"）。
- 敏感测试数据（密码、token）使用常量 fixture，不得与生产配置同名。

## 9. 前端测试

- 单元测试：**Vitest**（组件纯逻辑、store、工具函数）。
- E2E 测试：**Playwright**（与 §5 一致）。
- 前端单元覆盖率门槛沿用 80%。
- 组件测试优先测行为（渲染结果、交互），而非实现细节。

## 10. 验收清单

- [x] `pytest --cov-fail-under=80` 在 CI 通过（backend-test job，pyproject addopts 含门槛）。
- [x] schemathesis 覆盖所有 OpenAPI 端点（contract-test job 独立运行 `tests/test_api_contract.py`）。
- [x] Playwright 覆盖"登录→创建 Prompt→版本管理→回滚"路径，对 `dist/` 运行
      （`e2e/prompt-version-rollback.spec.ts`；CI 下 webServer 切换为 `vite build + preview`）。
- [x] L4 eval 框架存在，门槛 > 0.85 生效（llm-judge job，`continue-on-error` 非阻断）。
- [x] CI 中 L1–L3 阻断合并配置就绪（backend-test / contract-test / frontend-test 均为阻断 job）。
- [x] 测试数据采用 factory pattern，无共享状态（`tests/factories.py` + 每用例独立 fixture）。

### 10.1 已知待完善项（不阻塞 Phase 2 验收）

- **前端单元覆盖率未达 80%**：当前仅 `shared/api` + `shared/utils` 有单测（整体 ~7%）。
  CI 已收集覆盖率报告（`continue-on-error` 信息性），待补齐 stores/views 单测后转为硬门禁（Phase 5 领域深化）。
- **OpenAPI `gen:api` diff 校验未入 CI**：`src/shared/api/types.ts` 为手维护命名导出，
  而 `openapi-typescript@7` 生成 `components["schemas"]` 结构，二者不兼容——运行 gen:api 会破坏 type-check。
  OpenAPI 结构漂移暂由 L2 契约测试（`test_openapi_yaml_has_all_domains` 等）覆盖；
  全量 gen:api diff 强制待 types.ts 迁移到生成输出后启用。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次测试策略变更必须先更新本文件，再更新 eval，再更新代码。
