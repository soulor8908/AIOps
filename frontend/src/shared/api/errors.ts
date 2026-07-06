import { ApiError } from "./client";

/**
 * 将任意错误转为用户可读文案。
 *
 * 优先级：
 * 1. ApiError：按 status 映射统一文案（网络/超时/鉴权/权限/未找到/限流/服务器/网关）
 * 2. 普通 Error：用 message
 * 3. 兜底："未知错误"
 *
 * 设计取舍：不直接用后端返回的 detail message（可能是英文/技术化），
 * 而是按 status 给中文标准文案，便于非技术用户理解。后端 message 仍保留在
 * ApiError.payload 中，开发排障时可用。
 */
export function formatApiError(err: unknown): string {
  if (err instanceof ApiError) {
    return _mapApiError(err);
  }
  if (err instanceof Error) {
    return err.message || "未知错误";
  }
  if (typeof err === "string") return err;
  return "未知错误";
}

function _mapApiError(err: ApiError): string {
  // status=0：网络/超时（fetch 未抵达服务器）
  if (err.status === 0) {
    const payload = err.payload as { kind?: string } | undefined;
    if (payload?.kind === "timeout") return "请求超时，请稍后重试";
    if (payload?.kind === "network") return "网络连接异常，请检查网络后重试";
    if (payload?.kind === "sse-error") return `服务端流式错误：${err.message}`;
    return "网络异常，请稍后重试";
  }
  // HTTP 状态码映射
  switch (err.status) {
    case 400:
      return "请求参数错误";
    case 401:
      return "登录已过期，请重新登录";
    case 403:
      return "无权限执行此操作";
    case 404:
      return "请求的资源不存在";
    case 409:
      return "资源冲突（可能已存在）";
    case 413:
      return "请求体过大";
    case 422:
      return "请求参数校验失败";
    case 429:
      return "请求过于频繁，请稍后再试";
    case 500:
      return "服务器内部错误，请稍后重试";
    case 502:
    case 503:
      return "服务暂时不可用，请稍后重试";
    case 504:
      return "网关超时，请稍后重试";
    default:
      return err.message || `请求失败（HTTP ${err.status}）`;
  }
}
