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

export function setToken(token: string): void {
  if (token) {
    localStorage.setItem("token", token);
  } else {
    localStorage.removeItem("token");
  }
}

/**
 * 统一响应处理：401 清 token、非 2xx 抛 ApiError、204 返回 undefined。
 *
 * 抽离自 request/upload 共享，确保两条路径错误处理一致——之前 upload 不清 401 token、
 * 不处理网络/超时错误，导致文件上传失败时 token 残留 + 错误信息不一致。
 */
async function handleResponse<T>(res: Response): Promise<T> {
  // 401 表示 token 失效：清除本地 token，触发上层（路由守卫/用户 store）回到未认证态。
  if (res.status === 401) setToken("");

  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: res.statusText }));
    throw new ApiError(
      (err as { message?: string }).message || `HTTP ${res.status}`,
      res.status,
      err,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
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
  const url = `${API_BASE}${path}`;
  const res = await fetchWithTimeout(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...options.headers,
    },
  });
  return handleResponse<T>(res);
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
  const res = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: form,
  });
  return handleResponse<T>(res);
}
