// Karpathy style: ~80 lines, no axios, no black box.
// All HTTP calls go through this single function.
//
// Batch 6c：cookie 模式——token 存 httpOnly cookie（JS 不可读 → 防 XSS 偷取），
// fetch 全部 credentials:include 让浏览器自动携带 cookie。不再手动加
// Authorization header / 不再读写 localStorage。refresh 单飞锁改为返回
// boolean（成功后服务端已 set 新 cookie，前端无需感知 token 明文）。

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";

// fetch 超时（毫秒）。AbortController 触发后 fetch 抛 AbortError，
// 上层包装为 ApiError(status=0) 便于统一处理。
const DEFAULT_TIMEOUT_MS = 30_000;

// SSE 流式读取单次 read() 超时（毫秒）。SSE 是长连接，整体不能设固定超时，
// 但单次 reader.read() 若长时间无数据可能意味着连接挂起，给一个兜底超时。
// token 事件通常 < 5s 间隔，10s 无数据视为连接异常。
const SSE_READ_TIMEOUT_MS = 10_000;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly payload: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Batch 6c：401 且 refresh 失败时的回调——由 user store 注册，触发登出 +
 * 跳登录页。client 不直接依赖 store（避免循环依赖），通过此注入解耦。
 *
 * refresh 失败意味着 refresh_token cookie 也无效/过期，前端无法继续认证，
 * 必须清空本地会话状态（httpOnly cookie 只能由 /auth/logout 端点清除）。
 */
let _unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(cb: (() => void) | null): void {
  _unauthorizedHandler = cb;
}

// P1：refresh token 单飞锁。并发请求同时 401 时，仅第一个发起 /auth/refresh，
// 其余请求 await 同一个 Promise，避免 N 个 401 各自刷新导致旧 refresh token
// 被多次轮换（轮换是一次性的，第二次会用已被 revoke 的 token → 401）。
let _refreshPromise: Promise<boolean> | null = null;

/**
 * Batch 6c：用 refresh_token cookie 换新 access_token（单飞）。
 *
 * 服务端从 httpOnly cookie 读取 refresh_token，校验+轮换后 set 新 cookie。
 * 前端无需（也无法）读取 token 明文，仅感知成功/失败。
 *
 * @returns true=刷新成功（新 cookie 已下发）；false=失败（调用方应触发登出）
 */
async function refreshAccessToken(): Promise<boolean> {
  if (_refreshPromise) return _refreshPromise;
  _refreshPromise = (async () => {
    try {
      const res = await fetchWithTimeout(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        // body 留空：服务端优先读 cookie，body 仅 API 客户端兼容用
        body: JSON.stringify({}),
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      _refreshPromise = null;
    }
  })();
  return _refreshPromise;
}

/**
 * 统一响应处理：非 2xx 抛 ApiError、204 返回 undefined。
 *
 * 抽离自 request/upload 共享，确保两条路径错误处理一致。
 *
 * P1：成功分支也兜底 JSON 解析——反代返回 200+HTML 错误页或空体时 res.json()
 * 抛 SyntaxError，调用方 ``e instanceof ApiError`` 判断失效且丢失 status。
 */
async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: res.statusText }));
    throw new ApiError(
      (err as { message?: string }).message || `HTTP ${res.status}`,
      res.status,
      err,
    );
  }
  if (res.status === 204) return undefined as T;
  // P1：成功分支兜底 JSON 解析失败（200+HTML/空体），转为 ApiError 而非裸 SyntaxError
  try {
    return (await res.json()) as T;
  } catch (err) {
    throw new ApiError(
      "响应不是合法 JSON（可能为反代错误页或空响应）",
      res.status,
      { parseError: String(err) },
    );
  }
}

/**
 * P1 + Batch 6c：401 单次重试。access token 过期时调 /auth/refresh（服务端
 * 从 cookie 读 refresh_token 并 set 新 cookie），成功后重试原请求。
 *
 * refresh 失败 → 触发 unauthorized handler（user store 登出 + 跳登录），
 * 并抛原 401。仅重试一次，避免 refresh 端点本身 401 引发无限循环。
 */
async function requestWithRefreshRetry<T>(
  path: string,
  options: RequestInit,
): Promise<T> {
  const url = `${API_BASE}${path}`;
  // Batch 6c：credentials:include 携带 httpOnly cookie；不再加 Authorization header
  const doFetch = (init: RequestInit) =>
    fetchWithTimeout(url, {
      ...init,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...init.headers,
      },
    });
  try {
    return await handleResponse<T>(await doFetch(options));
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err;
    // 401 → 尝试 refresh（服务端 set 新 cookie）
    const ok = await refreshAccessToken();
    if (!ok) {
      // refresh 失败 → 通知 user store 登出（清本地状态 + 跳登录）
      _unauthorizedHandler?.();
      throw err;
    }
    // refresh 成功 → 重试一次（新 cookie 已由浏览器存储）
    return handleResponse<T>(await doFetch(options));
  }
}

/**
 * 包裹 fetch，将网络/超时错误统一转为 ApiError(status=0)。
 *
 * status=0 用于区分"请求未抵达服务器"（网络断开/DNS 失败/超时）与"服务器返回非 2xx"。
 * 调用方可据此给出更准确的用户提示（"网络异常，请检查连接" vs "服务器返回 500"）。
 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    // AbortError = 超时；其余 TypeError = 网络错误（fetch 在网络层失败时抛 TypeError）
    const isTimeout = err instanceof DOMException && err.name === "AbortError";
    throw new ApiError(
      isTimeout ? `请求超时 (${timeoutMs}ms)` : "网络错误，请检查连接",
      0,
      { kind: isTimeout ? "timeout" : "network", error: String(err) },
    );
  } finally {
    clearTimeout(timer);
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  return requestWithRefreshRetry<T>(path, options);
}

export const api = {
  get: <T>(path: string) => request<T>(path, { method: "GET" }),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  // OAuth2PasswordRequestForm 端点需 application/x-www-form-urlencoded（非 JSON）。
  // 后端 /auth/token 读取表单字段 username/password。
  postForm: <T>(path: string, fields: Record<string, string>) =>
    request<T>(path, {
      method: "POST",
      body: new URLSearchParams(fields).toString(),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    }),
};

// Upload helper for multipart/form-data (file uploads bypass JSON content-type).
// 后端 /knowledge-bases/{kb_id}/documents 要求 Form 字段 title + file。
//
// 与 request 共享 handleResponse/fetchWithTimeout + 401 refresh 重试。
// Batch 6c：credentials:include 携带 cookie；不手动设 Content-Type——浏览器
// 为 FormData 自动补充 boundary。
export async function upload<T>(path: string, file: File, title: string): Promise<T> {
  const form = new FormData();
  form.append("title", title);
  form.append("file", file);
  const doFetch = () =>
    fetchWithTimeout(`${API_BASE}${path}`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
  try {
    return await handleResponse<T>(await doFetch());
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err;
    // P1：401 尝试 refresh 后重试（与 request 一致）
    const ok = await refreshAccessToken();
    if (!ok) {
      _unauthorizedHandler?.();
      throw err;
    }
    return handleResponse<T>(await doFetch());
  }
}

// ===================== SSE 流式客户端 =====================
//
// EventSource 只支持 GET，后端 /agents/{id}/execute/stream 是 POST（带 ExecuteRequest body），
// 故用 fetch + ReadableStream + 手写 SSE 分帧解析。
//
// SSE 帧格式：`data: <payload>\n\n`（以双换行分隔）。payload 为 JSON 或字面量 `[DONE]`。
// 错误处理：服务端 error 事件转为 ApiError 抛出；连接异常断开由 reader.read() done 标志识别。
//
// Batch 6c：credentials:include 携带 cookie；移除 Authorization header。
// 初始 fetch 若 401，尝试一次 refresh（与 request 一致），失败则触发 unauthorized handler。

export type SSEEventHandler = (event: import("@/shared/api/types").SSEEvent) => void;

/**
 * 流式 POST 请求，按 SSE 分帧解析事件并回调 onEvent。
 *
 * @param path API 路径（不含 API_BASE）
 * @param body JSON 请求体
 * @param onEvent 每个事件的回调
 * @param signal 可选 AbortSignal，调用方可取消流
 * @returns Promise<void>，正常结束（收到 done/[DONE]）resolve，服务端 error 事件 reject ApiError
 */
export async function streamSSE(
  path: string,
  body: unknown,
  onEvent: SSEEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  const url = `${API_BASE}${path}`;
  const doFetch = () =>
    fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      credentials: "include",
      body: JSON.stringify(body),
      signal,
    });

  let res = await doFetch();
  // Batch 6c：初始 401 → 单次 refresh 重试（access token 可能刚过期）
  if (res.status === 401) {
    const ok = await refreshAccessToken();
    if (!ok) {
      _unauthorizedHandler?.();
      await handleResponse<unknown>(res); // 抛出 401 ApiError
      return;
    }
    res = await doFetch();
  }
  if (!res.ok) {
    // 非 2xx：复用 handleResponse 的错误解析逻辑（会抛 ApiError）
    await handleResponse<unknown>(res);
    return;
  }
  if (!res.body) {
    throw new ApiError("SSE 响应无 body", res.status, { kind: "no-body" });
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      // 单次 read 超时兜底：SSE 长连接若 10s 无任何数据视为挂起
      const readPromise = reader.read();
      const timeoutPromise = new Promise<{ done: true; value?: undefined }>(
        (resolve) => setTimeout(() => resolve({ done: true }), SSE_READ_TIMEOUT_MS),
      );
      const { done, value } = await Promise.race([readPromise, timeoutPromise]);
      if (done) break;
      if (!value) continue;

      buffer += decoder.decode(value, { stream: true });
      // SSE 帧以 \n\n 分隔，可能一次 read 包含多帧或不完整帧
      const frames = buffer.split("\n\n");
      // 最后一段可能不完整，保留到下次
      buffer = frames.pop() || "";

      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") return; // 终止标记
        try {
          const event = JSON.parse(payload) as import("@/shared/api/types").SSEEvent;
          if (event.type === "error") {
            throw new ApiError(
              event.message || "SSE 服务端错误",
              0,
              { kind: "sse-error", event },
            );
          }
          onEvent(event);
          if (event.type === "done") return; // done 事件后服务端会发 [DONE]，但提前退出也无妨
        } catch (err) {
          if (err instanceof ApiError) throw err;
          // JSON 解析失败：跳过该帧（容错，避免单帧损坏中断整个流）
          console.warn("SSE 帧解析失败:", payload, err);
        }
      }
    }
  } finally {
    // 确保释放 reader（即使提前 return / 抛错）
    reader.releaseLock();
  }
}
