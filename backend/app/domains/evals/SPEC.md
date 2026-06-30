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
- [ ] `create_eval` 拒绝空 cases
- [ ] `run_eval` 状态流转 pending → running → passed/failed
- [ ] `score = pass_count / total`，`PASSED` 当 `score ≥ 0.85`
- [ ] `judge_exact` 归一化空白后精确匹配
- [ ] `judge_llm` 输出无法解析 JSON 时返回 passed=False / score=0（不抛错）
- [ ] `judge_semantic` 余弦相似度 < 0.75 判定不通过
- [ ] 无 `predict_fn` 时用 case 的 `actual` / `expected` 自比对

## Data Models
- ORM `EvalRule`（`eval_rules` 表）：`id`(UUID)、`name`、`description`、`judge_type`、`expected`、`config`(JSONB)、`created_at`
- ORM `EvalJudge`（`eval_judges` 表）：`id`(UUID)、`name`、`judge_type`、`model_alias`、`prompt_template`、`config`(JSONB)、`created_at`
- ORM `EvalCase`（`eval_cases` 表）：`id`(UUID)、`name`、`input`、`expected`、`metadata`(JSONB)、`created_at`
- ORM `EvalRun`（`eval_runs` 表）：`id`(UUID)、`name`、`description`、`rules`(JSONB)、`cases`(JSONB)、`judge_type`、`status`、`results`(JSONB)、`pass_count`、`fail_count`、`score`(Float)、`started_at`、`finished_at`、`created_at`、`updated_at`
- Schemas：`EvalCaseInput` / `EvalRuleInput` / `EvalRunCreate` / `CaseResult`(case_name/input/expected/actual/passed/score/reason) / `EvalRunOut`
- `judge.py`：`JudgeResult`(passed/score/reason)、`judge_exact`、`judge_contains`、`judge_llm`(async, 解析 JSON)、`judge_semantic`(async, 复用 `embed_text` + 余弦)、`_normalize`（去多余空白 + lower）、`_cosine`
- service 关键行为：`create_eval` 写入 pending run；`run_eval` 置 running → 遍历 cases 调 `_predict` + `_judge_case` → 写 results/pass_count/fail_count/score → 置 passed/failed；`_predict` 默认回退 `case.actual` 或 `case.expected`

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
