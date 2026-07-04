# Feature: Agent Orchestrator

> 对齐实际实现：`models.py` / `router.py` / `service.py` / `executor.py`（前缀 `/api/v1`，agents 与 workflows 分别挂载）

## Goals
- Agent 定义：`system_prompt` + `model_alias` + `tools` + `max_turns` + `temperature`
- 工作流 DAG 编排（`nodes` + `edges` 以 JSONB 存储）
- Agent 执行：ReAct 循环（观察→思考→行动），解析 LLM 输出中的 ` ```tool_calls``` ` JSON 块
- 工作流执行：按节点顺序逐个执行 Agent，上下文在节点间传递
- 执行追踪：每轮记录 `thought` / `action` / `observation` / `tokens`

## Constraints
- DAG 最大 50 节点（`service.MAX_NODES = 50`，创建与执行均校验，超限抛 `LLMError`）
- 单次执行最大轮次 `MAX_TURNS = settings.agent_max_turns`（默认 10，可配到 50，B1）。`max_turns` 取值 1-`agent_max_turns`，Pydantic schema 用绝对上限 50 兜底
- `temperature` 0.0-2.0（默认 0.7）；`name` ≤ 128 字符
- LLM 调用 HTTP 超时默认 60s（`LLMClient(timeout=60.0)`）
- 工具类型枚举 `ToolType`：search / calculator / http / code / rag / custom
- 列表分页 `limit` 1-200
- 执行请求 `ExecuteRequest.input` 非空，`max_turns` 可选覆盖（仍受 ≤`agent_max_turns` 上限）

## Non-Goals
- 通用工作流引擎（BPMN / 复杂条件分支路由）
- 持久化状态机与断点续跑
- 多 Agent 实时协作
- 工具沙箱化安全执行

> **A2 Code Tool 安全策略**：`ToolType.code` 默认被 executor 拒绝（schema 不注入
> LLM）。`code` 工具暴露给 LLM 生成任意代码并通过 tool_call 触发执行——是脚枪。
> 仅当 `settings.agent_code_tool_enabled=True`（环境变量 `AGENT_CODE_TOOL_ENABLED`）
> 且调用方注入了沙箱化 `tool_executor` 时才允许。生产部署如需启用，必须配合
> 容器级沙箱（gVisor / Firecracker / nsjail）+ 资源限制（cgroups）+ 网络隔离。

## Success Criteria (Eval)
- [x] ReAct 循环在无工具调用时正确终止并返回最终答案
- [x] 达到 `max_turns` 仍无最终答案时返回截断提示且 `success=True`
- [x] 单工具执行异常被隔离捕获，不中断整体循环
- [x] DAG 节点 > 50 时创建与执行均报错
- [x] 每轮 `ExecutionTrace` 完整记录 thought/action/observation/tokens
- [x] 工具说明通过 `_build_tool_prompt` 正确注入 system prompt

> Eval 落地：`tests/test_agents_execution.py`（Phase 5 batch 2），10 测试覆盖全部 6 项
> Success Criteria。通过注入 mock `llm_client.chat`（AsyncMock side_effect）控制每轮 LLM
> 输出（tool_calls JSON 块 / 最终答案），验证 ReAct 终止、max_turns 截断、工具异常隔离、
> DAG 节点上限（创建路径抛 ValidationError + 执行路径抛 LLMError）、ExecutionTrace 字段
> 完整性、`_build_tool_prompt` 工具说明注入 system prompt 等行为。

## Data Models
- ORM `Agent`（`agents` 表）：`id`(UUID)、`name`、`description`、`system_prompt`、`model_alias`(默认 default)、`tools`(JSONB)、`max_turns`(默认 10)、`temperature`(默认 0.7)、`is_active`、`created_at`、`updated_at`
- ORM `Workflow`（`workflows` 表）：`id`(UUID)、`name`、`description`、`nodes`(JSONB)、`edges`(JSONB)、`is_active`、`created_at`、`updated_at`
- Schemas：
  - `ToolDef`(name/type/description/config) + `ToolType`(枚举)
  - `AgentCreate` / `AgentOut`
  - `AgentNode`(id/agent_id/name/inputs/is_entry/is_exit) + `WorkflowEdge`(source/target/condition)
  - `WorkflowDef` / `WorkflowOut`
  - `ExecutionTrace`(turn/thought/action/observation/tokens)
  - `ExecutionResult`(agent_id/workflow_id/final_answer/traces/total_tokens/success/error)
  - `ExecuteRequest`(input/max_turns/context)
- `executor.py`：
  - `AgentExecutor.run()` 实现 ReAct 循环，`turns = min(max_turns or agent.max_turns, agent.max_turns)`
  - `_run_turn`：调 LLM → `parse_tool_calls_json` 解析 → 无调用则结束返回答案；有调用则 `_execute_tools` 后追加 assistant/tool 消息继续
  - `_compress_context`（B4）：每轮开始前用 `_estimate_tokens` 估算消息总 token，超 `settings.agent_context_compress_tokens`（默认 4000）时用 LLM 摘要历史中段（head + summary + tail），压缩事件记入 traces（`thought="[context_compressed]"`，`observation` 记前后 token/消息数）。摘要失败降级为截断（仍记 trace）。tail_size 自适应（消息少时 < 6）确保 ≥4 条消息即可压缩。
  - `execute_workflow_dag`：按拓扑分层并发执行，`context[node_id]` 与 `__input__` 传递，节点无 `agent_id` 时直传输入。A3：`condition_evaluator` 注入后求值 `WorkflowEdge.condition`（LLM judge），求值失败保守放行。B2：同层独立节点 `asyncio.gather` 并发执行。

## API Endpoints
前缀 `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agents` | Agent 列表（limit/offset） |
| POST | `/agents` | 创建 Agent |
| GET | `/agents/{agent_id}` | Agent 详情 |
| POST | `/agents/{agent_id}/execute` | 执行 Agent（ReAct） |
| GET | `/workflows` | 工作流列表 |
| POST | `/workflows` | 创建工作流 |
| POST | `/workflows/{workflow_id}/execute` | 执行工作流 DAG |

## Error Cases
- Agent / Workflow 不存在 → `NotFoundError` (404)
- DAG 节点为空 → `ValidationError` (422, "工作流无节点")
- DAG 节点 > 50 → `ValidationError` (422)
- LLM 调用 HTTP 失败 → `LLMError` (502)
- 未知工具名 → 记录 `[未知工具: name]` 观察结果，不抛错
- 工具执行异常 → 记录 `[{name} 错误] {exc}` 观察结果，不中断循环
- `tool_executor` 未配置 → 记录 `[tool executor 未配置，跳过工具调用]`
- 达到 `max_turns` 截断 → `ExecutionResult.success = False`（视为执行失败而非成功）
