# Feature: Eval Suite

> 对齐实际实现：`models.py` / `router.py` / `service.py` / `judge.py`（前缀 `/api/v1/evals`）

## Goals
- 评估定义：`EvalRun` 携带 rules + cases + judge_type，UUID 主键
- 基于规则测试：exact（归一化后精确匹配）、contains（子串包含）
- LLM-as-judge：调用 LLM 对回答打分（0-1）
- 语义判官：embed 后余弦相似度
- 回归检测：通过 run 历史（score / pass_count / fail_count）对比评估随时间变化
- 同步执行评估并记录逐条 case 结果

## Constraints
- 判官类型：exact / contains / llm / semantic（`JudgeType` 枚举）
- 判官通过阈值：`judge_llm` score ≥ 0.5、`judge_semantic` score ≥ 0.75
- EvalRun 状态：pending / running / passed / failed / error（`EvalStatus` 枚举）
- run 整体 `PASSED` 当 `score >= 0.85`，否则 `FAILED`（`score = pass_count / total`）
- `cases` 必须非空（service 层强制，否则 `ValidationError`）
- `name` ≤ 128 字符；列表分页 `limit` 1-200
- `judge_llm` 要求 LLM 输出可解析 JSON `{"score":..., "reason":...}`

## Non-Goals
- 通用测试框架（pytest 风格 fixture / 参数化）
- 异步分布式并行评估
- 自动生成测试用例
- CI pipeline 集成

## Success Criteria (Eval)
- [x] `create_eval` 拒绝空 cases
- [x] `run_eval` 状态流转 pending → running → passed/failed
- [x] `run_eval` 任一 case 抛异常时状态置为 `error` 并附 `finished_at`
- [x] `score = pass_count / total`，`PASSED` 当 `score ≥ 0.85`
- [x] `judge_exact` 归一化空白后精确匹配
- [x] `judge_llm` 输出无法解析 JSON 时返回 passed=False / score=0（不抛错）
- [x] `judge_semantic` 余弦相似度 < 0.75 判定不通过
- [x] 无 `predict_fn` 时用 case 的 `actual` / `expected` 自比对

## Eval 落地记录

测试文件：`backend/tests/test_eval_suite.py`（12 tests，覆盖全部 8 项 Success Criteria）
补充：`backend/app/domains/evals/tests/test_evals.py`（13 tests，领域内嵌单元测试，覆盖 judge 纯函数 + service 基础流程）

| SC | 测试 | 策略 |
|----|------|------|
| 1 | `test_create_eval_rejects_empty_cases` | `EvalRunCreate(cases=[])` 抛 `ValidationError` |
| 2 | `test_run_eval_status_pending_to_running_to_passed` / `test_run_eval_status_pending_to_running_to_failed` | 在 `predict_fn` 内 `session.get(EvalRun, run.id)` 读取执行期状态断言 `RUNNING`；最终断言 `PASSED`（2/2 全过）或 `FAILED`（2/3 ≈ 0.667 < 0.85） |
| 3 | `test_run_eval_error_status_has_finished_at` | `predict_fn` 抛 `RuntimeError`，断言异常透传；`session.rollback()` 后用独立 session 读取，验证 `status=error` + `finished_at is not None`（service 通过 `_persist_error_status` 独立事务落库） |
| 4 | `test_run_eval_score_at_threshold_passes` / `test_run_eval_score_below_threshold_fails` | 临界值精确验证：6/7 ≈ 0.857 ≥ 0.85 → PASSED；5/6 ≈ 0.833 < 0.85 → FAILED |
| 5 | `test_judge_exact_normalizes_whitespace` | `_normalize`（strip + lower + `re.sub(r"\s+", " ")`) 后比对：`"Hello   World"` / `"Hello\n\tWorld"` / `"  hello  "` 均匹配 `"hello world"`；`"foo" vs "bar"` score=0.0 |
| 6 | `test_judge_llm_unparseable_json_returns_failed_zero_score` / `test_judge_llm_missing_score_field_returns_zero` | stub `LLMClient.chat` 返回非 JSON 文本 / 缺 score 字段 JSON，断言 `passed=False` / `score=0.0` / reason 含「无法解析」，不抛错 |
| 7 | `test_judge_semantic_below_threshold_fails` | stub embedder 构造可控向量：正交（cosine=0.0）/ 完全相同（1.0）/ cos(45°)≈0.707，断言 < 0.75 时 `passed=False` |
| 8 | `test_run_eval_without_predict_fn_uses_case_actual` / `test_run_eval_without_predict_fn_falls_back_to_expected` | 无 `predict_fn`：case 顶层 `actual="wrong"` → 与 expected 不匹配 → FAILED；无 actual → 回退 `expected` → 自比对全过 → PASSED |

## Data Models
- ORM `EvalRule`（`eval_rules` 表）：`id`(UUID)、`name`、`description`、`judge_type`、`expected`、`config`(JSONB)、`created_at`
- ORM `EvalJudge`（`eval_judges` 表）：`id`(UUID)、`name`、`judge_type`、`model_alias`、`prompt_template`、`config`(JSONB)、`created_at`
- ORM `EvalCase`（`eval_cases` 表）：`id`(UUID)、`name`、`input`、`expected`、`metadata`(JSONB)、`created_at`
- ORM `EvalRun`（`eval_runs` 表）：`id`(UUID)、`name`、`description`、`rules`(JSONB)、`cases`(JSONB)、`judge_type`、`status`、`results`(JSONB)、`pass_count`、`fail_count`、`score`(Float)、`started_at`、`finished_at`、`created_at`、`updated_at`
- Schemas：`EvalCaseInput` / `EvalRuleInput` / `EvalRunCreate` / `CaseResult`(case_name/input/expected/actual/passed/score/reason) / `EvalRunOut`
- `judge.py`：`JudgeResult`(passed/score/reason)、`judge_exact`、`judge_contains`、`judge_llm`(async, 解析 JSON)、`judge_semantic`(async, 复用 `embed_text` + 余弦)、`_normalize`（去多余空白 + lower）、`_cosine`
- service 关键行为：`create_eval` 写入 pending run；`run_eval` 置 running → 遍历 cases 调 `_predict` + `_judge_case` → 写 results/pass_count/fail_count/score → 置 passed/failed；`_predict` 默认回退 `case.actual` 或 `case.expected`；任一 case 判定抛异常时 `run.status=error`、`finished_at=now()` 并重新抛出

> **C5 分层采样**：`EvalSample` ORM 新增 `priority` 列（默认 0，索引
> `idx_eval_samples_priority`）。`execute_agent` 采样钩子按启发式计算 priority：
> 长输入（>`online_eval_priority_input_len_threshold`）/ self-heal 触发 /
> eval_score<0.7 各 +1。priority>0 的样本采样率放大
> `online_eval_sample_rate_boost` 倍（`effective_rate = min(base * boost, 1.0)`），
> 确保稀有但易暴露回归的流量不被均匀采样稀释。`list_samples` 支持 `priority_min`
> 过滤 + `priority DESC, sampled_at DESC` 排序；`run_online_eval` 按 priority DESC
> 选取，先评高价值样本。迁移 `0007_add_eval_sample_priority`。

## API Endpoints
前缀 `/api/v1/evals`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/evals` | eval 列表（limit/offset） |
| POST | `/evals` | 创建 eval（cases 必填） |
| GET | `/evals/{eval_id}` | eval 详情 |
| POST | `/evals/{eval_id}/run` | 同步执行 eval |

## Error Cases
- eval 不存在 → `NotFoundError` (404)
- cases 为空 → `ValidationError` (422)
- 未知 judge_type → `LLMError` (502)
- LLM 判官调用失败 → `LLMError` (502)
- LLM 输出无法解析 JSON → 返回 passed=False / score=0（不抛错）
