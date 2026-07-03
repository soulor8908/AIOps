// Karpathy style: ~80 lines, no axios, no black box.
// All HTTP calls go through this single function.

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";

// fetch 超时（毫秒）。AbortController 触发后 fetch 抛 AbortError，
// 上层包装为 ApiError(status=0) 便于统一处理。
const DEFAULT_TIMEOUT_MS = 30_000;

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

function getToken(): string {
  return localStorage.getItem("token") || "";
}

function getRefreshToken(): string {
  return localStorage.getItem("refresh_token") || "";
}

export function setToken(token: string): void {
  if (token) {
    localStorage.setItem("token", token);
  } else {
    localStorage.removeItem("token");
  }
}

function setRefreshToken(token: string): void {
  if (token) {
    localStorage.setItem("refresh_token", token);
  } else {
    localStorage.removeItem("refresh_token");
  }
}

// P1：refresh token 单飞锁。并发请求同时 401 时，仅第一个发起 /auth/refresh，
// 其余请求 await 同一个 Promise，避免 N 个 401 各自刷新导致旧 token 被多次轮换。
let _refreshPromise: Promise<string | null> | null = null;

/**
 * 用 refresh token 换新 access token（单飞）。
 *
 * 成功返回新 access token 并写入 localStorage；失败返回 null（调用方应 logout）。
 * 并发调用复用同一个 Promise，避免重复刷新。
 */
async function refreshAccessToken(): Promise<string | null> {
  if (_refreshPromise) return _refreshPromise;
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;
  _refreshPromise = (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { access_token: string; refresh_token: string };
      setToken(data.access_token);
      setRefreshToken(data.refresh_token);
      return data.access_token;
    } catch {
      return null;
    } finally {
      _refreshPromise = null;
    }
  })();
  return _refreshPromise;
}

/**
 * 统一响应处理：非 2xx 抛 ApiError、204 返回 undefined。
 *
 * 抽离自 request/upload 共享，确保两条路径错误处理一致——之前 upload 不清 401 token、
 * 不处理网络/超时错误，导致文件上传失败时 token 残留 + 错误信息不一致。
 *
 * P1：成功分支也兜底 JSON 解析——反代返回 200+HTML 错误页或空体时 res.json()
 * 抛 SyntaxError，调用方 ``e instanceof ApiError`` 判断失效且丢失 status。
 *
 * 注意：401 不在此清 token，由 request 层负责尝试 refresh 后再决定是否清除，
 * 避免可刷新的 401 误清 token 导致用户被登出。
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
 * P1：401 单次重试。access token 过期时用 refresh token 换新 token 并重试原请求。
 * refresh 失败（无 refresh token / refresh 端点拒绝）则清 token 并抛原 401。
 *
 * 仅重试一次，避免 refresh 端点本身 401 引发无限循环。
 */
async function requestWithRefreshRetry<T>(
  path: string,
  options: RequestInit,
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const doFetch = (init: RequestInit) =>
    fetchWithTimeout(url, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
        ...init.headers,
      },
    });
  try {
    return await handleResponse<T>(await doFetch(options));
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err;
    // 401 → 尝试 refresh
    const newToken = await refreshAccessToken();
    if (!newToken) {
      // refresh 失败 → 清 token，触发路由守卫跳登录
      setToken("");
      setRefreshToken("");
      throw err;
    }
    // refresh 成功 → 用新 token 重试一次
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
// 与 request 共享 handleResponse/fetchWithTimeout：401 清 token、超时/网络错误
// 抛 ApiError(status=0)、非 2xx 抛 ApiError(status, payload)。不手动设置
// Content-Type——浏览器为 FormData 自动补充 boundary。
export async function upload<T>(path: string, file: File, title: string): Promise<T> {
  const form = new FormData();
  form.append("title", title);
  form.append("file", file);
  const doFetch = () =>
    fetchWithTimeout(`${API_BASE}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
      body: form,
    });
  try {
    return await handleResponse<T>(await doFetch());
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 401) throw err;
    // P1：401 尝试 refresh 后重试（与 request 一致）
    const newToken = await refreshAccessToken();
    if (!newToken) {
      setToken("");
      setRefreshToken("");
      throw err;
    }
    return handleResponse<T>(await doFetch());
  }
}
