# 横切关注点 Spec — 数据库迁移（Migration）

> Version: v0.1.0-alpha | Date: 2026-06-30
> Scope: Schema 真源策略、Alembic 流程、init.sql 边界、漂移检测
> 关联: SPEC.md#迁移、backend/SPEC.md（ORM）、testing.spec.md（一致性校验 CI）

---

## 1. 目标

消除 AIOps Console 数据库 schema 的多源真源问题：
- 单一真源：ORM（SQLAlchemy `Base.metadata`）。
- 迁移可追溯、可回滚、可自动检测漂移。
- `init.sql` 不再承担表结构定义职责。

## 2. 单一真源策略

- **Schema 真源 = ORM**（SQLAlchemy 2.0 `Base.metadata`）。
- 所有表结构变更**必须**先修改 ORM 模型，再生成迁移。
- 禁止以 `init.sql`、手写 DDL、数据库客户端直接改表结构作为真源。
- ORM 模型与数据库实际 schema 的偏差由 CI 一致性校验拦截（见 §8）。

## 3. 迁移工具

- 工具：**Alembic**（SQLAlchemy 官方迁移工具）。
- 配置：`alembic.ini` 指向 `migrations/` 目录。
- `env.py` 须从 `Base.metadata` 读取 target_metadata，保证 autogenerate 准确。

## 4. init.sql 的职责边界

`backend/init.sql` **仅用于**以下三类操作，禁止定义业务表结构：

1. **创建扩展**：`CREATE EXTENSION IF NOT EXISTS vector;`、`CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`
2. **种子数据**：初始 admin 用户、默认配置等（仅可重入的 `INSERT ... ON CONFLICT DO NOTHING`）。
3. **性能优化索引**：Alembic 难以表达或需人工调优的复合索引（须注释说明理由）。

> 业务表结构（CREATE TABLE / ALTER TABLE）一律走 Alembic，不得出现在 `init.sql`。

## 5. 迁移流程（强制）

```
1. 修改 ORM 模型（domains/*/models.py）
2. alembic revision --autogenerate -m "<描述>"
3. review 生成的迁移脚本（检查 upgrade/downgrade 对称、op 类型正确）
4. alembic upgrade head（本地验证）
5. 提交 ORM 模型 + 迁移脚本到同一 PR
```

- 自动生成的脚本**必须人工 review**：autogenerate 可能漏检约束改名、server_default 变更等。
- 每个迁移脚本必须同时实现 `upgrade()` 与 `downgrade()`。
- 迁移脚本须包含空 `revision` 之外的清晰 `down_revision` 链，禁止分叉多线。

## 6. 版本控制

- `migrations/` 目录纳入 git，与代码同提交。
- **禁止**手写 DDL 修改表结构绕过 Alembic（紧急 hotfix 除外，且须事后补迁移脚本并记录）。
- 迁移文件命名沿用 Alembic 默认（`<revision>_<slug>.py`），slug 用 snake_case 描述意图。
- 禁止删除已合并的迁移脚本；历史修正以新迁移叠加。

## 7. 回滚策略

- Alembic `downgrade` 必须可用：`alembic downgrade -1`、`alembic downgrade <revision>`。
- **生产环境回滚需 DBA review**：破坏性 downgrade（如 drop column）可能丢数据，须评审后执行。
- 数据迁移类（非纯结构）downgrade 若无法安全还原数据，须在脚本注释明确"downgrade 仅还原结构，数据不可逆"，并触发告警。

## 8. 一致性校验（CI）

- CI 中运行 **ORM metadata vs 数据库实际 schema 的 diff 检查**：
  - 起一个临时数据库，`alembic upgrade head` 到最新，再与 `Base.metadata.create_all()` 结果对比。
  - 若存在 diff，说明迁移脚本与 ORM 不一致，CI 失败。
- 配合 L1 测试（SQLite in-memory）发现"ORM 改了但没生成迁移"的情况。
- 校验脚本纳入 CI 流程，PR 必过。

## 9. 已知漂移修复清单

当前仓库存在以下历史漂移，须按 §5 流程逐项补迁移修复：

| # | 漂移现象 | 根因 | 修复动作 |
|---|----------|------|----------|
| 1 | `evals` 领域缺 `eval_rules` 表 | ORM 未定义该模型 | 在 `domains/evals/models.py` 补 `EvalRule` 模型 → autogenerate → 补迁移 |
| 2 | `evals` 领域缺 `eval_judges` 表 | ORM 未定义该模型 | 补 `EvalJudge` 模型 → 迁移 |
| 3 | `evals` 领域缺 `eval_cases` 表 | ORM 未定义该模型 | 补 `EvalCase` 模型 → 迁移 |
| 4 | `users` 表无对应 ORM 模型 | 表由 `init.sql` 手写 DDL 创建 | 在 `domains/auth/`（或 core）补 `User` ORM 模型 → 迁移接管，逐步退役 `init.sql` 中的 users DDL |

- 修复优先级：#4（users 无 ORM）影响认证链路，优先处理；#1–#3 影响 Eval Suite 功能完整性。
- 每项修复独立 PR，便于 review 与回滚。

## 10. 验收清单

- [ ] `init.sql` 不再包含 `CREATE TABLE` 业务表结构。
- [ ] 所有表均有对应 ORM 模型（含 users、eval_*）。
- [ ] CI 一致性校验通过（ORM vs DB 无 diff）。
- [ ] 最新迁移 `upgrade` + `downgrade` 均可执行。
- [ ] 已知漂移清单全部清零或有明确修复 PR。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次 schema 变更必须先更新本文件（如流程调整），再修改 ORM，再生成迁移。
