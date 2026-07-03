# Feature: Model Router

> 对齐实际实现：`models.py` / `router.py` / `service.py`（前缀 `/api/v1/models`）

## Goals
- 多模型统一配置：以 `alias` 唯一标识，封装 provider / model_name / 参数 / 成本
- 请求路由：支持 direct / round_robin / least_cost / latency 策略选择候选模型
- Fallback 策略：primary 失败时按 `priority` 升序依次降级尝试
- 成本追踪：按 token 用量与单价计算单次调用成本

## Constraints
- providers：openai / anthropic / azure_openai / local / custom（`ModelProvider` 枚举）
- 路由策略：direct / round_robin / least_cost / latency（`RoutingStrategy` 枚举）
- `alias` ≤ 64 字符且唯一；`model_name` ≤ 128 字符
- `max_tokens` 1-200000（默认 4096）；`temperature` 0.0-2.0（默认 0.7）
- `priority` 0-1000（默认 100，越小越优先）；成本字段 `Numeric(10,6)`，默认 0
- `messages` 1-100 条；`role` 1-16 字符、`content` 非空
- 列表分页 `limit` 1-500，支持 `active_only` 过滤
- `api_key` 通过 `api_key_env` 指定的环境变量读取

## Non-Goals
- 模型训练 / fine-tune
- 模型自托管推理服务
- 实时负载均衡监控
- 跨 provider 的统一流式响应
- 自动 provider 健康探测

## Success Criteria (Eval)
- [x] direct 策略仅返回 primary
- [x] round_robin 策略在 active 候选中按调用次序轮转，primary 仍居首
- [x] least_cost 按 (input+output) 单价升序排列候选
- [x] latency 策略按 priority 升序（priority 越小视为延迟越低）
- [x] primary 失败时按候选列表降级，并在响应中标记 `fallback_used`
- [x] 成本计算精度到 6 位小数（`quantize(0.000001)`）
- [x] 所有候选均失败时抛 `LLMError` 并附 last_error
- [x] provider 为 `azure_openai`/`custom` 且未配置 `api_base` 时调用前抛 `LLMError`（避免静默失败）

> Eval 落地：`tests/test_models_routing.py`（Phase 5 batch 1），9 测试覆盖全部 8 项
> Success Criteria。通过 mock `LLMClient.chat` 控制候选成功/失败，验证路由策略排序、
> fallback 降级、成本量化、全失败错误传播、azure 无 api_base 跳过等行为。

## Data Models
- ORM `ModelConfig`（`model_configs` 表）：`id`(UUID)、`alias`(unique)、`provider`、`model_name`、`api_base`、`api_key_env`、`max_tokens`(默认 4096)、`temperature`(默认 0.7)、`cost_per_1k_input`(Numeric(10,6))、`cost_per_1k_output`(Numeric(10,6))、`is_active`(默认 True)、`priority`(默认 100)、`created_at`、`updated_at`
- Enums：`ModelProvider`(openai/anthropic/local/azure_openai/custom)、`RoutingStrategy`(direct/round_robin/least_cost/latency)
- Schemas：
  - `ModelConfigCreate` / `ModelConfigUpdate` / `ModelConfigOut`
  - `ChatMessage`(role/content) / `ChatRequest`(messages/temperature/max_tokens/strategy)
  - `ChatResponse`(content/model/alias/usage/cost/fallback_used)
- service 关键行为：
  - `list_models` 按 `priority` 升序
  - `route_model` 按策略生成候选列表（primary 始终居首）
  - `chat_completion` 遍历候选，逐个 `LLMClient.chat`，失败 `continue`，成功返回并标记 `fallback_used = idx > 0`
  - `_to_llm_config` 按 provider 取 api_key（openai/anthropic 走 settings，其余走 `api_key_env`）
  - `_compute_cost` = in_tokens/1000 * input 单价 + out_tokens/1000 * output 单价

## API Endpoints
前缀 `/api/v1/models`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/models` | 配置列表（active_only / limit / offset） |
| POST | `/models` | 新增配置 |
| GET | `/models/{alias}` | 按 alias 获取 |
| PUT | `/models/{alias}` | 更新配置 |
| DELETE | `/models/{alias}` | 删除配置 |
| POST | `/models/{alias}/chat` | 走该模型对话（含 fallback） |

## Error Cases
- 模型配置不存在 → `NotFoundError` (404)
- `alias` 唯一冲突 → DB 约束冲突 (409)
- 无可用候选模型 → `LLMError` (502)
- 所有候选均失败 → `LLMError` (502, 附 last_error)
- 参数越界 → Pydantic 校验 (422)
