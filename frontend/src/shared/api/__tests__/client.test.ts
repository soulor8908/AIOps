import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request, setUnauthorizedHandler, upload } from "../client";

/** 用给定实现替换全局 fetch，返回 mock 以便断言调用参数。 */
function mockFetch(
  impl: (url: string, init?: RequestInit) => Promise<Response> | Response,
) {
  const fn = vi.fn(impl);
  globalThis.fetch = fn as unknown as typeof fetch;
  return fn;
}

function jsonResponse(body: unknown, init: ResponseInit = { status: 200 }): Response {
  return new Response(JSON.stringify(body), {
    status: init.status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  setUnauthorizedHandler(null);
});

describe("request — cookie 模式", () => {
  it("成功时返回解析后的 JSON，fetch 带 credentials:include 且无 Authorization header", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ ok: true, data: 42 }));
    const result = await request<{ ok: boolean; data: number }>("/ping");
    expect(result).toEqual({ ok: true, data: 42 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1]!;
    expect(init.credentials).toBe("include");
    const headers = init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBeUndefined();
    expect(headers["Content-Type"]).toBe("application/json");
  });

  it("失败时抛出 ApiError 并携带状态码与 payload", async () => {
    mockFetch(async () => jsonResponse({ detail: "boom" }, { status: 500 }));
    await expect(request("/fail")).rejects.toThrow(ApiError);
    await expect(request("/fail")).rejects.toMatchObject({
      status: 500,
      payload: { detail: "boom" },
    });
  });

  it("401 + refresh 成功 → 重试原请求一次（共 2 次 fetch：原请求 + refresh + 重试）", async () => {
    let originalCalled = 0;
    const fetchMock = mockFetch(async (url) => {
      // /auth/refresh 返回 200（cookie 模式下 refresh 成功）
      if (url.includes("/auth/refresh")) {
        return jsonResponse({ access_token: "new", refresh_token: "new" });
      }
      originalCalled += 1;
      // 第一次 401，第二次（重试）200
      if (originalCalled === 1) {
        return jsonResponse({ detail: "unauthorized" }, { status: 401 });
      }
      return jsonResponse({ ok: true });
    });
    const result = await request<{ ok: boolean }>("/secret");
    expect(result).toEqual({ ok: true });
    // 3 次 fetch：原请求 401 + refresh + 重试 200
    expect(fetchMock).toHaveBeenCalledTimes(3);
    // refresh 请求带 credentials:include + POST
    const refreshCall = fetchMock.mock.calls[1];
    expect(refreshCall[0]).toContain("/auth/refresh");
    expect(refreshCall[1]!.method).toBe("POST");
    expect(refreshCall[1]!.credentials).toBe("include");
  });

  it("401 + refresh 失败 → 触发 unauthorized handler 并抛 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    mockFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        return jsonResponse({ detail: "invalid" }, { status: 401 });
      }
      return jsonResponse({ detail: "unauthorized" }, { status: 401 });
    });
    await expect(request("/secret")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("非 401 错误不触发 refresh 重试", async () => {
    const fetchMock = mockFetch(async () =>
      jsonResponse({ detail: "server error" }, { status: 500 }),
    );
    await expect(request("/fail")).rejects.toMatchObject({ status: 500 });
    // 仅 1 次 fetch（500 不重试）
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("api 方法", () => {
  it("api.get 发起 GET 请求并带 credentials:include", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({}));
    await api.get("/items");
    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
  });

  it("api.post 发起 POST 并以 JSON 发送 body", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ id: 1 }));
    await api.post("/items", { name: "x" });
    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect(JSON.parse(init.body as string)).toEqual({ name: "x" });
  });

  it("api.put 发起 PUT 并以 JSON 发送 body", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ id: 1 }));
    await api.put("/items/1", { name: "y" });
    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ name: "y" });
  });

  it("api.del 发起 DELETE 请求", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({}));
    await api.del("/items/1");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
  });

  it("api.postForm 以 application/x-www-form-urlencoded 发送", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({}));
    await api.postForm("/auth/token", { username: "a@b.com", password: "pw" });
    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/x-www-form-urlencoded",
    );
    expect(init.body).toBe("username=a%40b.com&password=pw");
  });
});

describe("upload — cookie 模式", () => {
  it("以 FormData 发送文件（POST），credentials:include，不手动设 Content-Type", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ url: "/files/1" }));
    const file = new File(["hello"], "f.txt", { type: "text/plain" });
    await upload("/upload", file, "doc-title");

    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(init.body).toBeInstanceOf(FormData);

    const fd = init.body as FormData;
    expect(fd.get("file")).toBe(file);
    expect(fd.get("title")).toBe("doc-title");
    // upload 不设置 headers——浏览器为 FormData 自动补充 Content-Type+boundary
    const headers = init.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBeUndefined();
    // cookie 模式：无 Authorization header
    expect(headers?.["Authorization"]).toBeUndefined();
  });

  it("401 + refresh 失败 → 触发 unauthorized handler 并抛 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    mockFetch(async (url) => {
      if (url.includes("/auth/refresh")) {
        return jsonResponse({ detail: "invalid" }, { status: 401 });
      }
      return jsonResponse({ detail: "unauthorized" }, { status: 401 });
    });
    const file = new File(["x"], "f.txt", { type: "text/plain" });
    await expect(upload("/upload", file, "t")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("非 2xx 时抛 ApiError 携带状态码与 payload", async () => {
    mockFetch(async () => jsonResponse({ detail: "too large" }, { status: 413 }));
    const file = new File(["x"], "f.txt", { type: "text/plain" });
    await expect(upload("/upload", file, "t")).rejects.toMatchObject({
      status: 413,
      payload: { detail: "too large" },
    });
  });
});

describe("fetchWithTimeout 错误包装", () => {
  it("网络错误（fetch 抛 TypeError）转为 ApiError(status=0, kind=network)", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(request("/anywhere")).rejects.toMatchObject({
      status: 0,
      payload: { kind: "network" },
    });
  });

  it("AbortError（超时）转为 ApiError(status=0, kind=timeout)", async () => {
    mockFetch(async () => {
      const err: unknown = new DOMException("Aborted", "AbortError");
      throw err;
    });
    await expect(request("/slow")).rejects.toMatchObject({
      status: 0,
      payload: { kind: "timeout" },
    });
  });
});
