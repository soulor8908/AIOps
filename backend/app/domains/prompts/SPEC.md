# Feature: Prompt Studio

> 对齐实际实现：`models.py` / `router.py` / `service.py`（前缀 `/api/v1/prompts`）

## Goals
- 提供 Prompt 的 CRUD 管理（创建、查询、更新、删除）
- 版本管理：每次内容变更自动递增 `version_num`，保留完整线性版本历史
- 版本回滚：以目标版本内容追加创建新版本（append-only，不覆盖历史）
- 版本 diff：对任意两个版本号做行级 unified diff

## Constraints
- Prompt 内容最大 64KB（`content` 字段 `max_length=65536`，schema 层校验）
- 单 Prompt 最大 100 版本（`service.MAX_VERSIONS = 100`，超限抛 `ConflictError`）
- Prompt `name` 最大 128 字符
- `variables` 为字符串列表，按 `a-zA-Z0-9_` 命名约定存储（schema 层为自由字符串）
- 主键使用 UUID；版本号 `version_num` 单调递增（基于 `max(version_num)+1`）
- 列表分页 `limit` 1-200、`offset` ≥ 0
- 删除为硬删除，级联删除其全部版本

## Non-Goals
- Git 式分支管理（仅线性版本链）
- 多人实时协作编辑
- 多模态 Prompt（仅文本）
- Prompt 执行 / 变量渲染（由 agents / models 领域消费）

## Success Criteria (Eval)
- [x] 版本创建 P99 < 200ms
- [x] 回滚后 `current_version` 内容与目标版本 100% 一致
- [x] 并发创建版本不产生重复 `version_num`
- [x] diff 接口对相同版本号正确报错，对差异版本返回 added/removed/unified_diff
- [x] 删除 Prompt 时级联删除其全部版本
- [x] 创建 Prompt 时自动写入 `version_num=1` 并设为 current

> Eval 落地：`tests/test_prompts_versioning.py`（Phase 5 batch 4），8 测试覆盖全部 6 项
> Success Criteria。经 `client` fixture 的 session_factory 直接调用 service 层
> （create_prompt / create_version / rollback_prompt / diff_versions / delete_prompt），
> 验证版本自增逻辑（连续创建无重复 version_num）、回滚后 current_version 内容一致、
> diff 同版本报错 + 差异版本返回 added/removed/unified_diff、删除级联清空版本、
> 创建自动写入 v1 并设为 current。
>
> 范围说明：ROADMAP 5.1 提及"A/B 测试、变量模板渲染"在 SPEC 中显式声明为 Non-Goal
> （"Prompt 执行 / 变量渲染（由 agents / models 领域消费）"），故 5.1 的实际交付为
> 版本管理能力 eval 覆盖，A/B 测试与变量渲染不在本领域范围内。

## Data Models
- ORM `Prompt`（`prompts` 表）：`id`(UUID)、`name`、`description`、`current_version_id`(FK→prompt_versions.id, ondelete SET NULL)、`is_active`(默认 True)、`created_at`、`updated_at`；`versions` 关系（cascade all, delete-orphan，按 `version_num` 倒序）
- ORM `PromptVersion`（`prompt_versions` 表）：`id`(UUID)、`prompt_id`(FK, ondelete CASCADE)、`version_num`(int)、`content`(Text)、`variables`(JSONB)、`change_note`、`created_by`、`created_at`
- Pydantic schemas：
  - `PromptCreate`（含初始 content/variables）
  - `PromptUpdate`（元信息 name/description/is_active）
  - `PromptOut`（含 versions 列表，`from_orm_with_versions`）
  - `PromptVersionCreate` / `PromptVersionOut`
  - `DiffResult`（from_version / to_version / added_lines / removed_lines / unified_diff）
- service 关键行为：`create_version` 计数校验 + `max+1` 自增；`rollback_prompt` 复用 `create_version` 追加新版本（change_note=`rollback to vN`）；`diff_versions` 用 `difflib.unified_diff` 行级比较

## API Endpoints
前缀 `/api/v1/prompts`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/prompts` | 列表，支持 `q`(名称模糊) / `limit` / `offset` |
| POST | `/prompts` | 创建 Prompt（含初始 version_num=1） |
| GET | `/prompts/{prompt_id}` | 详情（含 versions） |
| PUT | `/prompts/{prompt_id}` | 更新元信息 |
| DELETE | `/prompts/{prompt_id}` | 硬删除（级联 versions） |
| GET | `/prompts/{prompt_id}/versions` | 版本列表 |
| POST | `/prompts/{prompt_id}/versions` | 新增版本（version_num 自增） |
| POST | `/prompts/{prompt_id}/versions/{version_id}/rollback` | 回滚（追加新版本） |
| GET | `/prompts/{prompt_id}/diff?from=&to=` | 版本 diff |

## Error Cases
- Prompt / 版本不存在 → `NotFoundError` (404)
- 版本数达 100 上限 → `ConflictError` (409)
- diff 的 `from` 与 `to` 相同 → `ValidationError` (422)
- `content` 为空或超 64KB → Pydantic 校验 (422)
- diff 指定版本号不存在 → `NotFoundError` (404)
