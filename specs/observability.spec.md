# 横切关注点 Spec — 可观测性（Observability）

> Version: v0.1.0 | Date: 2026-07-03
> Scope: 结构化日志、请求追踪、指标、告警、前端监控
> 关联: SPEC.md#可观测性、errors.spec.md（500 兜底日志）、deployment.spec.md（健康检查）

---

## 1. 目标

为 AIOps Console 建立统一可观测性基线：
- 日志结构化、可关联、可检索。
- 每个请求可端到端追踪。
- 关键指标（延迟、错误率、LLM 成本）可度量、可告警。
- 前端用户侧体验可感知。

## 2. 结构化日志

### 2.1 格式
- 日志一律 **JSON 格式**（一行一条），便于日志系统解析与检索。
- 禁止纯文本人类日志混入生产日志流。

### 2.2 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `timestamp` | string (ISO 8601, UTC) | 事件时间戳 |
| `level` | string | 日志级别（见 §3） |
| `logger` | string | logger 名称（模块/领域） |
| `message` | string | 简短描述 |
| `request_id` | string (UUID) | 请求追踪 ID（见 §4），无上下文时省略 |
| `user_id` | string/int | 触发请求的用户 ID，未认证时省略 |
| `latency_ms` | number | 请求处理耗时（毫秒），仅请求结束日志携带 |

- 额外业务字段（如 `prompt_id`、`model`、`token_usage`）按需附加，沿用 snake_case。
- 禁止在日志中打印密钥、完整 token、用户密码（见 `security.spec.md`§8/§9）。

## 3. 日志级别

| 级别 | 用途 | 默认环境 |
|------|------|----------|
| DEBUG | 详细诊断信息（SQL、变量值） | 开发 |
| INFO | 正常运行事件（请求、启动、定时任务） | 生产 |
| WARNING | 异常但可恢复（降级、重试成功、配额接近上限） | 生产 |
| ERROR | 需关注的错误（请求失败、未捕获异常兜底） | 生产 |
| CRITICAL | 系统不可用（启动失败、依赖全断） | 生产 |

- 生产默认 `INFO`，可通过 `LOG_LEVEL` 环境变量调整。
- 禁止用 `print()` 替代日志。
- ERROR/CRITICAL 必须包含足够上下文（request_id、输入摘要）以复盘。

## 4. 请求追踪（Request Tracing）

- 每个入站请求分配 **request_id**（UUID v4）。
- 分配时机：最外层中间件，在路由解析前。
- request_id 贯穿整条日志链路：所有该请求产生的日志、下游服务调用、数据库查询日志均携带同一 request_id。
- 响应头回传 `X-Request-ID`，便于前端/用户上报问题时关联。
- 前端发起的请求可携带上游 `X-Request-ID`（若由网关下发），后端尊重传入值或生成新值。

## 5. 指标（Metrics）

### 5.1 必备指标

| 指标 | 类型 | 维度 | 用途 |
|------|------|------|------|
| 请求数（request_count） | counter | endpoint, method, status | 流量趋势 |
| 延迟分布（request_latency） | histogram | endpoint | P50/P95/P99 |
| 错误率（error_rate） | gauge/rate | endpoint | 健康度 |
| LLM token 消耗（llm_tokens） | counter | model, direction(in/out) | 成本核算 |
| LLM 成本（llm_cost） | counter | model | 成本告警 |
| LLM prompt cache 命中（llm_cached_tokens） | counter | model | cache 命中率监控（C4） |

- 指标采集不阻塞请求路径（异步上报）。
- 指标命名沿用 `module_metric_unit` 风格，全小写下划线。

## 6. 告警规则

| 告警 | 触发条件 | 严重度 |
|------|----------|--------|
| 错误率告警 | 5xx 错误率 > 5%（滑动 5 分钟） | 高 |
| 延迟告警 | P99 延迟 > 2s（滑动 5 分钟） | 中 |
| LLM 成本告警 | LLM 成本超阈值（日/小时配额） | 中 |
| 可用性告警 | `/health` 持续失败 | 严重 |

- 告警须可路由到值班渠道，含 request_id/endpoint 上下文。
- 阈值通过配置管理，便于按环境调整。

## 7. 前端监控

| 指标 | 采集方式 | 用途 |
|------|----------|------|
| 页面加载时间（FCP/LCP） | Performance API | 体验基线 |
| API 错误率 | `client.ts` 拦截 `ApiError` 上报 | 前端可见故障 |
| 用户操作追踪 | 关键交互埋点（路由切换、核心动作） | 行为分析 |

- 前端错误上报携带 `X-Request-ID`（若 API 失败）与用户上下文。
- 前端监控数据脱敏，禁止上报请求体中的敏感字段。

## 8. 验收清单

- [x] 生产日志为 JSON，含 §2.2 全部字段。
- [x] 每请求分配 request_id，贯穿日志并回传响应头。
- [x] ERROR/CRITICAL 不泄露密钥与堆栈到响应（仅日志）。
- [x] 请求数、延迟、错误率、LLM token/成本指标已采集。
- [x] 错误率 > 5%、P99 > 2s、LLM 成本超阈值告警已配置。
- [ ] 前端页面加载、API 错误率、关键操作追踪就绪。

### 8.1 落地记录

- **Phase 4 batch 3**（分支 `feat/phase4-observability`，合并到 main）：
  - §4/§8 request_id 贯穿链路端到端验证：`test_request_id_propagates_to_request_log`
    携带 X-Request-ID 请求，断言该值同时出现在响应头与 "request completed" 日志记录
    （ContextVar set → RequestContextFilter 注入 → 日志携带，全链路）。
  - §5.1 错误率指标可观测验证：`test_4xx/5xx_recorded_in_request_count` 断言 404/500
    请求被记入 request_count（含 status 维度），error_rate 可由 Prometheus rate() 派生。
  - §6 告警规则落地（G5 缺口）：新增 `ops/prometheus/alerts.yml`（4 条规则：可用性
    /health 失败 critical、5xx 错误率 >5% high、P99 >2s medium、LLM 小时成本 >$50
    medium）+ `ops/prometheus/scrape.yml`（ServiceMonitor 抓取 /metrics）。
  - §8 验收清单 5/6 勾选（前端监控 §7 留待 Phase 5 前端加固）。测试总数 362 全绿。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次可观测性变更必须先更新本文件，再更新采集代码与告警配置。
