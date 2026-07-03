# Feature: Conversation Analytics

> 对齐实际实现：`models.py` / `router.py` / `service.py`（前缀 `/api/v1/analytics`）

## Goals
- 对话记录存储与检索：`Conversation` 聚合 token / 成本，`Message` 记录逐条消息
- Token 消耗统计：按对话、模型、时间维度聚合 token 与成本
- 对话质量度量：基于平均延迟、token 效率、成本等聚合指标评估对话质量
- 仪表盘：返回全局聚合指标 + 活跃模型排行 + 按天对话趋势

## Constraints
- 批处理优先（非实时监控）
- 仪表盘时间窗口 `days` 1-90（默认 7）
- 活跃模型排行取 top 10（按 token 总量降序）
- 列表分页 `limit` 1-200
- `total_cost` 精度 `Numeric(12,6)`
- 跨域引用（`user_id` / `agent_id`）仅存 UUID 且建索引，不在 ORM 层耦合其他领域 metadata（FK 由 init.sql 维护）

## Non-Goals
- 实时监控 / 流式指标推送
- 对话内容审计与合规过滤
- 用户画像与行为分析
- 自动告警

## Success Criteria (Eval)
- [x] 列表支持按 `user_id` 过滤，并预加载 messages
- [x] dashboard 正确聚合 total_conversations / total_messages / total_tokens / total_cost
- [x] `avg_messages_per_conversation` = total_messages / total_conversations（空集为 0）
- [x] 活跃模型按 token 总量降序取 top 10
- [x] `conversations_last_7d` 按天聚合 count 与 tokens
- [x] `avg_latency_ms` 仅对非空 `latency_ms` 求平均

> Eval 落地：`tests/test_analytics_aggregation.py`（Phase 5 batch 3），8 测试覆盖全部 6 项
> Success Criteria。经 `client` fixture 获得独立 SQLite in-memory DB，通过 session_factory
> 直接 seed Conversation / Message 数据，再调用 service 层（list_conversations /
> get_dashboard_metrics）断言聚合结果。验证 user_id 过滤 + selectinload 预加载、四个总量
> 聚合、avg_messages 计算（含空集 0）、活跃模型 top 10 降序截断、按天聚合 count/tokens、
> avg_latency_ms 排除 NULL 等行为。

## Data Models
- ORM `Conversation`（`conversations` 表）：`id`(UUID)、`user_id`(UUID, index)、`agent_id`(UUID, index)、`model_alias`、`title`、`metadata`(JSONB)、`total_tokens`(默认 0)、`total_cost`(Numeric(12,6), 默认 0)、`created_at`、`updated_at`；`messages` 关系（cascade all, delete-orphan，按 `created_at` 升序）
- ORM `Message`（`messages` 表）：`id`(UUID)、`conversation_id`(FK, CASCADE, index)、`role`(user/assistant/system/tool)、`content`、`tokens_in`、`tokens_out`、`latency_ms`、`model_alias`、`created_at`
- Schemas：
  - `MessageOut`
  - `ConversationOut`（含 messages 列表）
  - `DashboardMetrics`(total_conversations / total_messages / total_tokens / total_cost / avg_messages_per_conversation / avg_latency_ms / active_models / conversations_last_7d)
- service 关键行为：
  - `list_conversations` / `get_conversation` 均 `selectinload(messages)`
  - `get_dashboard_metrics` 聚合 count/sum/coalesce；`_active_models` group by model_alias 取 top 10；`_conversations_by_day` 用 `date_trunc('day', ...)` 按天聚合

## API Endpoints
前缀 `/api/v1/analytics`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/analytics/conversations` | 对话列表（user_id / limit / offset） |
| GET | `/analytics/conversations/{conversation_id}` | 对话详情（含 messages） |
| GET | `/analytics/dashboard` | 仪表盘指标（days） |

## Error Cases
- 对话不存在 → `NotFoundError` (404)
- `days` 越界 → Pydantic 校验 (422)
- 空数据集 → 聚合返回 0 / 空列表（不抛错）
