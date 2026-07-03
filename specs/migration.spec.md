# 横切关注点 Spec — 数据库迁移（Migration）

> Version: v0.1.0 | Date: 2026-07-03
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

`backend/init.sql` 作为 PostgreSQL 容器 initdb 脚本，在容器首次启动时执行（**早于**应用与 Alembic）。因此它**只能包含无表依赖的语句**。

- **init.sql 仅保留**：`CREATE EXTENSION IF NOT EXISTS ...`（`vector` / `pgcrypto` / `citext`）。
- **业务表结构**（`CREATE TABLE` / `ALTER TABLE`）：一律走 Alembic 迁移。
- **索引**（含 HNSW 向量索引、复合索引）：在 ORM 模型 `__table_args__` 中声明（`Index(...)`），由 Alembic 迁移从 `Base.metadata` 派生，确保 ORM 为单一真源。
- **种子数据**：走 Alembic 数据迁移（`op.bulk_insert`），保证其在表创建之后执行且版本可追溯。
- **触发器**（如 `touch_updated_at`）：**不再使用**。ORM 模型 `updated_at` 列已声明 `onupdate=func.now()`，由 SQLAlchemy 在 UPDATE 语句中渲染 `now()`，等价覆盖触发器语义且无 PG 专属依赖。

> 历史上 init.sql 同时定义表/索引/触发器/种子，与 ORM 双真源漂移。现已收敛为「扩展-only」，其余全部移交 Alembic。

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

历史漂移修复状态（2026-07-03 更新）：

| # | 漂移现象 | 根因 | 修复状态 |
|---|----------|------|----------|
| 1 | `evals` 领域缺 `eval_rules` 表 | ORM 未定义该模型 | ✅ 已补 `EvalRule` ORM（`domains/evals/models.py`）+ Alembic 迁移接管 |
| 2 | `evals` 领域缺 `eval_judges` 表 | ORM 未定义该模型 | ✅ 已补 `EvalJudge` ORM + 迁移 |
| 3 | `evals` 领域缺 `eval_cases` 表 | ORM 未定义该模型 | ✅ 已补 `EvalCase` ORM + 迁移 |
| 4 | `users` 表无对应 ORM 模型 | 表由 `init.sql` 手写 DDL 创建 | ✅ 已补 `User` ORM（`domains/auth/models.py`）+ 迁移接管 |
| 5 | init.sql 与 ORM 双真源（表/索引/触发器/种子重复定义） | init.sql 承担表结构定义 | ✅ init.sql 收敛为「扩展-only」，表/索引移交 Alembic，触发器移除（ORM `onupdate` 替代），种子改数据迁移 |
| 6 | `conversations` 跨域 FK（→users/agents）仅 init.sql 强制，ORM 未声明 | ORM 刻意回避跨域 metadata 耦合 | ✅ 以字符串 `ForeignKey("users.id"/"agents.id")` 声明，无 metadata 耦合且恢复 DB 级约束 |

- 已知漂移清单已全部清零。

## 10. 验收清单

- [x] `init.sql` 不再包含 `CREATE TABLE` 业务表结构（仅保留 `CREATE EXTENSION`）。
- [x] 所有表均有对应 ORM 模型（含 users、eval_*）。
- [x] 索引（含 HNSW、复合索引）在 ORM `__table_args__` 声明，ORM 为单一真源。
- [x] 种子数据走 Alembic 数据迁移（`op.bulk_insert`）。
- [ ] CI 一致性校验通过（ORM vs DB 无 diff）— 由 CI job `migration-consistency` 验证。
- [ ] 最新迁移 `upgrade` + `downgrade` 均可执行 — 由 CI job `migration-consistency` 验证。
- [x] 已知漂移清单全部清零。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次 schema 变更必须先更新本文件（如流程调整），再修改 ORM，再生成迁移。
