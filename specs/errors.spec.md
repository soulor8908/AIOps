# 横切关注点 Spec — 错误处理（Error Handling）

> Version: v0.1.0-alpha | Date: 2026-06-30
> Scope: 全栈（backend FastAPI + frontend Vue/Pinia）统一错误处理
> 关联: SPEC.md#错误处理、agents.md#5-禁止模式

---

## 1. 目标

为 AIOps Console 提供一致的错误处理契约：
- 后端所有错误以**统一 JSON 格式**返回，前端无需猜测字段。
- 错误码（`error`）机器可读，HTTP 状态码语义明确，`message` 人类可读。
- 前端通过单一 `ApiError` 类型承接，UI 层只关心 `code` + `message`。

## 2. 错误响应统一格式

所有非 2xx 响应**必须**使用以下 JSON 结构，禁止散落的 `{detail: [...]}`、`{msg: "..."}`、纯字符串等格式。

```json
{
  "error": "<error_code>",
  "message": "<human_readable_message>",
  "detail": <optional_any>
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `error` | string | 是 | 机器可读错误码，snake_case，见 §4 |
| `message` | string | 是 | 面向终端用户的简短描述，禁止暴露内部实现细节（如堆栈、SQL） |
| `detail` | any | 否 | 结构化补充信息（如字段级校验错误数组），仅在该信息对调用方有用时才提供 |

**约束**：
- `error` 一律小写 snake_case，禁止大写、驼峰、空格。
- `message` 不得包含密钥、内部路径、堆栈信息。
- 缺省 `detail` 字段必须省略，禁止返回 `"detail": null`。

## 3. HTTP 状态码规范

| 状态码 | 语义 | 触发场景 |
|--------|------|----------|
| 400 | 请求错误（Bad Request） | 请求体格式错误、缺少必填参数且无法归类到 422 |
| 401 | 未认证（Unauthorized） | 缺少/无效/过期 token |
| 403 | 无权限（Forbidden） | 已认证但角色不足 |
| 404 | 不存在（Not Found） | 资源 id 不存在 |
| 409 | 冲突（Conflict） | 唯一约束冲突、版本冲突、状态机非法转移 |
| 422 | 验证错误（Unprocessable Entity） | Pydantic 校验失败、业务规则校验失败 |
| 429 | 限流（Too Many Requests） | 触发限流（见 `security.spec.md`§限流） |
| 500 | 服务器错误（Internal Server Error） | 未捕获异常兜底 |

**规则**：
- 401 vs 403：未认证 → 401；已认证但越权 → 403。禁止用 401 表达权限不足。
- 400 vs 422：请求无法解析 → 400；可解析但语义校验失败 → 422。
- 禁止将业务错误伪装成 500；500 仅用于真正未预期的内部异常。

## 4. 错误码命名规范

- 命名风格：`snake_case`，全小写。
- 结构建议：`<domain>_<reason>` 或通用 `<reason>`，如 `not_found`、`validation_error`。
- 禁止使用 HTTP 状态码字面值作为错误码（如 `error: "500"`）。

**通用错误码清单**（领域可按需扩展，但须沿用风格）：

| error_code | HTTP | message 示例 |
|------------|------|--------------|
| `bad_request` | 400 | 请求格式错误 |
| `token_missing` | 401 | 未提供认证凭据 |
| `token_invalid` | 401 | 认证凭据无效 |
| `token_expired` | 401 | 认证凭据已过期 |
| `permission_denied` | 403 | 无权访问该资源 |
| `not_found` | 404 | 资源不存在 |
| `conflict` | 409 | 资源冲突 |
| `validation_error` | 422 | 输入校验失败 |
| `rate_limited` | 429 | 请求过于频繁，请稍后重试 |
| `internal_error` | 500 | 服务器内部错误 |

## 5. 后端实现

### 5.1 AppError 基类与子类

后端定义统一异常体系，所有业务异常**必须**继承 `AppError`，禁止直接抛 `HTTPException(detail=str)`。

```python
class AppError(Exception):
    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "服务器内部错误"
    detail: Any | None = None

    def to_response(self) -> dict:
        resp = {"error": self.error_code, "message": self.message}
        if self.detail is not None:
            resp["detail"] = self.detail
        return resp


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"
    message = "资源不存在"


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"
    message = "输入校验失败"


class AuthenticationError(AppError):
    status_code = 401
    error_code = "token_invalid"
    message = "认证凭据无效"


class PermissionError(AppError):
    status_code = 403
    error_code = "permission_denied"
    message = "无权访问该资源"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"
    message = "资源冲突"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limited"
    message = "请求过于频繁，请稍后重试"
```

- 子类可通过构造参数覆盖默认 `message` / `detail`。
- 业务层抛出异常时优先使用语义子类，而非裸 `AppError`。

### 5.2 异常处理器注册

在 `app/main.py` 注册以下处理器，将异常统一转换为 §2 格式：

```python
@app.exception_handler(AppError)
async def app_error_handler(request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_response())
```

### 5.3 FastAPI RequestValidationError 处理器

FastAPI 默认对 Pydantic 校验失败返回 `{"detail": [...]}`，**必须**改写为统一格式：

```python
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def validation_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "输入校验失败",
            "detail": exc.errors(),  # 保留字段级错误数组
        },
    )
```

- 转换后 `detail` 保留 FastAPI 原始字段级错误（含 `loc`、`msg`、`type`），供前端精确定位。
- HTTP 状态码固定 422。

### 5.4 全局 500 兜底处理器

任何未被捕获的异常**必须**被兜底，避免堆栈泄露：

```python
@app.exception_handler(Exception)
async def unhandled_handler(request, exc: Exception):
    logger.exception("unhandled error", request_id=request.state.request_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "服务器内部错误",
        },
    )
```

- 兜底处理器**禁止**将 `str(exc)` 写入响应体。
- 必须记录完整异常日志（含 `request_id`），见 `observability.spec.md`。
- 生产环境必须启用此处理器；开发环境可附加 `detail` 用于调试。

## 6. 前端处理

### 6.1 client.ts 统一捕获

`shared/api/client.ts`（约 50 行，基于 fetch）统一拦截所有 HTTP 错误，抛出 `ApiError`：

```typescript
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public detail?: unknown,
    public status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// fetch 封装内：
if (!response.ok) {
  const body = await response.json().catch(() => ({}));
  throw new ApiError(
    body.error ?? "internal_error",
    body.message ?? "服务器内部错误",
    body.detail,
    response.status,
  );
}
```

- 网络层错误（`fetch` reject）也必须包装为 `ApiError("network_error", "网络请求失败")`。
- UI 层禁止捕获原始 `Response` 或解析状态码，统一 catch `ApiError`。

### 6.2 Store / 组件层处理

- `store.ts` 通过 `api.ts` 调用，`api.ts` 内部使用 `client.ts`，错误以 `ApiError` 向上冒泡（见 `agents.md` 修正条款与 `frontend/SPEC.md`§4.4）。
- Store 决定哪些错误需要转换为 UI state（如 toast），哪些直接 rethrow。
- 组件层只消费 `ApiError.code` 做分支（如 `token_expired` → 跳转登录），禁止解析 HTTP 状态码。

## 7. 验收清单

- [ ] 所有非 2xx 响应符合 §2 格式，无 `{detail: [...]}`、纯字符串残留。
- [ ] `RequestValidationError` 被改写为 422 统一格式。
- [ ] 500 兜底处理器存在且不泄露堆栈。
- [ ] 前端 `client.ts` 统一抛 `ApiError`，无裸 `fetch` 错误冒泡。
- [ ] L2 契约测试覆盖每个错误码的状态码与 schema。

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次错误处理变更必须先更新本文件，再更新 eval，再更新代码。
