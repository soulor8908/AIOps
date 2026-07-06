import { describe, expect, it } from "vitest";
import { ApiError } from "../client";
import { formatApiError } from "../errors";

describe("formatApiError", () => {
  it("ApiError status=0 kind=timeout → 超时文案", () => {
    const err = new ApiError("timeout", 0, { kind: "timeout" });
    expect(formatApiError(err)).toBe("请求超时，请稍后重试");
  });

  it("ApiError status=0 kind=network → 网络文案", () => {
    const err = new ApiError("net", 0, { kind: "network" });
    expect(formatApiError(err)).toBe("网络连接异常，请检查网络后重试");
  });

  it("ApiError status=0 kind=sse-error → SSE 错误文案", () => {
    const err = new ApiError("stream broken", 0, { kind: "sse-error" });
    expect(formatApiError(err)).toBe("服务端流式错误：stream broken");
  });

  it("ApiError status=0 无 kind → 通用网络文案", () => {
    const err = new ApiError("x", 0, {});
    expect(formatApiError(err)).toBe("网络异常，请稍后重试");
  });

  it("ApiError 401 → 未授权文案", () => {
    const err = new ApiError("unauthorized", 401, {});
    expect(formatApiError(err)).toBe("登录已过期，请重新登录");
  });

  it("ApiError 403 → 无权限文案", () => {
    const err = new ApiError("forbidden", 403, {});
    expect(formatApiError(err)).toBe("无权限执行此操作");
  });

  it("ApiError 404 → 未找到文案", () => {
    const err = new ApiError("not found", 404, {});
    expect(formatApiError(err)).toBe("请求的资源不存在");
  });

  it("ApiError 429 → 限流文案", () => {
    const err = new ApiError("rate limit", 429, {});
    expect(formatApiError(err)).toBe("请求过于频繁，请稍后再试");
  });

  it("ApiError 500 → 服务器文案", () => {
    const err = new ApiError("boom", 500, {});
    expect(formatApiError(err)).toBe("服务器内部错误，请稍后重试");
  });

  it("ApiError 504 → 网关超时文案", () => {
    const err = new ApiError("gateway", 504, {});
    expect(formatApiError(err)).toBe("网关超时，请稍后重试");
  });

  it("ApiError 502/503 → 服务不可用文案", () => {
    expect(formatApiError(new ApiError("x", 502, {}))).toBe("服务暂时不可用，请稍后重试");
    expect(formatApiError(new ApiError("x", 503, {}))).toBe("服务暂时不可用，请稍后重试");
  });

  it("ApiError 413 → 请求体过大", () => {
    expect(formatApiError(new ApiError("x", 413, {}))).toBe("请求体过大");
  });

  it("ApiError 422 → 参数校验失败", () => {
    expect(formatApiError(new ApiError("x", 422, {}))).toBe("请求参数校验失败");
  });

  it("ApiError 400 → 请求参数错误", () => {
    expect(formatApiError(new ApiError("x", 400, {}))).toBe("请求参数错误");
  });

  it("ApiError 409 → 资源冲突", () => {
    expect(formatApiError(new ApiError("x", 409, {}))).toBe("资源冲突（可能已存在）");
  });

  it("ApiError 未知状态码 → fallback 到 message", () => {
    const err = new ApiError("weird", 418, {});
    expect(formatApiError(err)).toBe("weird");
  });

  it("ApiError 未知状态码且空 message → HTTP 状态码兜底", () => {
    const err = new ApiError("", 418, {});
    expect(formatApiError(err)).toBe("请求失败（HTTP 418）");
  });

  it("普通 Error → message", () => {
    expect(formatApiError(new Error("custom error"))).toBe("custom error");
  });

  it("普通 Error 空 message → 兜底", () => {
    const e = new Error("");
    expect(formatApiError(e)).toBe("未知错误");
  });

  it("字符串错误 → 原样返回", () => {
    expect(formatApiError("string error")).toBe("string error");
  });

  it("null/undefined → 兜底", () => {
    expect(formatApiError(null)).toBe("未知错误");
    expect(formatApiError(undefined)).toBe("未知错误");
  });
});
