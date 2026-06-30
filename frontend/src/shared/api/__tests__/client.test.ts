import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request, setToken, upload } from "../client";

/** 应用读取 auth token 的 localStorage key（与 shared/stores/user 对齐）。 */
const TOKEN_KEY = "token";

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
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("request", () => {
  it("成功时返回解析后的 JSON", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ ok: true, data: 42 }));
    const result = await request<{ ok: boolean; data: number }>("/ping");
    expect(result).toEqual({ ok: true, data: 42 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1];
    // 未显式指定 method 时，fetch 默认 GET
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("失败时抛出 ApiError 并携带状态码与 payload", async () => {
    mockFetch(async () => jsonResponse({ detail: "boom" }, { status: 500 }));
    await expect(request("/fail")).rejects.toThrow(ApiError);
    await expect(request("/fail")).rejects.toMatchObject({
      status: 500,
      payload: { detail: "boom" },
    });
  });

  it("401 时触发 token 清除", async () => {
    setToken("valid-token");
    expect(localStorage.getItem(TOKEN_KEY)).toBe("valid-token");
    mockFetch(async () => jsonResponse({ detail: "unauthorized" }, { status: 401 }));
    await expect(request("/secret")).rejects.toMatchObject({ status: 401 });
    // request 在 401 时应清除本地 token
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe("api 方法", () => {
  it("api.get 发起 GET 请求", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({}));
    await api.get("/items");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("GET");
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
});

describe("setToken", () => {
  it("将 token 写入 localStorage", () => {
    setToken("abc");
    expect(localStorage.getItem(TOKEN_KEY)).toBe("abc");
  });

  it("传入空串时移除 token", () => {
    setToken("abc");
    setToken("");
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe("upload", () => {
  it("以 FormData 发送文件（POST），不手动设置 Content-Type", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({ url: "/files/1" }));
    const file = new File(["hello"], "f.txt", { type: "text/plain" });
    await upload("/upload", file);

    const init = fetchMock.mock.calls[0][1]!;
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);

    const fd = init.body as FormData;
    expect(fd.get("file")).toBe(file);
    // upload 仅设置 Authorization，Content-Type 由浏览器为 FormData 自动补充 boundary
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });
});
