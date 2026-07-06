import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, setUnauthorizedHandler, streamSSE } from "../client";
import type { SSEEvent } from "@/shared/api/types";

/** 用给定 ReadableStream 构造 Response，替换全局 fetch。 */
function mockFetchSSE(
  frames: string[],
  init: ResponseInit = { status: 200 },
) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  const fn = vi.fn(async () =>
    new Response(stream, {
      ...init,
      headers: { "content-type": "text/event-stream", ...init.headers },
    }),
  );
  globalThis.fetch = fn as unknown as typeof fetch;
  return fn;
}

beforeEach(() => {
  setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  setUnauthorizedHandler(null);
});

describe("streamSSE — cookie 模式", () => {
  it("解析 token + done 事件并回调 onEvent", async () => {
    mockFetchSSE([
      'data: {"type":"token","content":"hello"}\n\n',
      'data: {"type":"token","content":" world"}\n\n',
      'data: {"type":"done","result":{"agent_id":null,"workflow_id":null,"final_answer":"hello world","traces":[],"total_tokens":10,"success":true,"error":null}}\n\n',
      "data: [DONE]\n\n",
    ]);
    const events: SSEEvent[] = [];
    await streamSSE("/agents/x/execute/stream", { input: "hi" }, (e) => events.push(e));
    expect(events).toHaveLength(3);
    expect(events[0]).toEqual({ type: "token", content: "hello" });
    expect(events[1]).toEqual({ type: "token", content: " world" });
    expect(events[2].type).toBe("done");
  });

  it("解析 tool + observation 事件", async () => {
    mockFetchSSE([
      'data: {"type":"tool","name":"search","args":{"q":"x"}}\n\n',
      'data: {"type":"observation","content":"result"}\n\n',
      'data: {"type":"done","result":{"agent_id":null,"workflow_id":null,"final_answer":"a","traces":[],"total_tokens":5,"success":true,"error":null}}\n\n',
    ]);
    const events: SSEEvent[] = [];
    await streamSSE("/p", {}, (e) => events.push(e));
    expect(events[0]).toEqual({
      type: "tool",
      name: "search",
      args: { q: "x" },
    });
    expect(events[1]).toEqual({ type: "observation", content: "result" });
  });

  it("收到 [DONE] 终止标记时正常结束", async () => {
    mockFetchSSE([
      'data: {"type":"token","content":"x"}\n\n',
      "data: [DONE]\n\n",
    ]);
    const events: SSEEvent[] = [];
    await streamSSE("/p", {}, (e) => events.push(e));
    expect(events).toHaveLength(1);
  });

  it("error 事件转为 ApiError 抛出", async () => {
    mockFetchSSE([
      'data: {"type":"error","message":"internal error"}\n\n',
    ]);
    const events: SSEEvent[] = [];
    let caught: unknown = null;
    try {
      await streamSSE("/p", {}, (e) => events.push(e));
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).message).toBe("internal error");
    expect(events).toHaveLength(0);
  });

  it("非 2xx 响应抛 ApiError", async () => {
    const fn = vi.fn(async () =>
      new Response(JSON.stringify({ detail: "forbidden" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      }),
    );
    globalThis.fetch = fn as unknown as typeof fetch;
    await expect(
      streamSSE("/p", {}, () => {}),
    ).rejects.toMatchObject({ status: 403 });
  });

  it("多个事件在同一 chunk 中正确分帧", async () => {
    // 一次 chunk 包含多个完整帧 + 一个不完整帧，下次 chunk 补全
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"type":"token","content":"a"}\n\ndata: {"type":"token","content":"b"}\n\n',
          ),
        );
        controller.enqueue(
          encoder.encode(
            'data: {"type":"done","result":{"agent_id":null,"workflow_id":null,"final_answer":"ab","traces":[],"total_tokens":2,"success":true,"error":null}}\n\n',
          ),
        );
        controller.close();
      },
    });
    globalThis.fetch = vi.fn(async () =>
      new Response(stream, { status: 200 }),
    ) as unknown as typeof fetch;

    const events: SSEEvent[] = [];
    await streamSSE("/p", {}, (e) => events.push(e));
    expect(events).toHaveLength(3);
    expect((events[0] as { content: string }).content).toBe("a");
    expect((events[1] as { content: string }).content).toBe("b");
    expect(events[2].type).toBe("done");
  });

  it("JSON 解析失败的帧被跳过（容错）", async () => {
    mockFetchSSE([
      'data: {"type":"token","content":"good"}\n\n',
      "data: not-json\n\n",
      'data: {"type":"done","result":{"agent_id":null,"workflow_id":null,"final_answer":"good","traces":[],"total_tokens":1,"success":true,"error":null}}\n\n',
    ]);
    const events: SSEEvent[] = [];
    await streamSSE("/p", {}, (e) => events.push(e));
    // 跳过 bad 帧，保留 good + done
    expect(events).toHaveLength(2);
  });

  it("POST 请求带 credentials:include + JSON body，无 Authorization header", async () => {
    const fn = mockFetchSSE(["data: [DONE]\n\n"]);
    await streamSSE("/agents/x/execute/stream", { input: "hi" }, () => {});
    expect(fn).toHaveBeenCalledTimes(1);
    const callArgs = fn.mock.calls[0] as unknown as [string, RequestInit];
    const init = callArgs[1];
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    const headers = init.headers as Record<string, string>;
    // Batch 6c：cookie 模式不送 Authorization header
    expect(headers["Authorization"]).toBeUndefined();
    expect(headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ input: "hi" });
  });

  it("初始 401 + refresh 成功 → 重试 SSE 请求", async () => {
    const encoder = new TextEncoder();
    const goodStream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"type":"done","result":{"agent_id":null,"workflow_id":null,"final_answer":"ok","traces":[],"total_tokens":1,"success":true,"error":null}}\n\n',
          ),
        );
        controller.close();
      },
    });
    let sseCalled = 0;
    const fn = vi.fn(async (url: string) => {
      if (url.includes("/auth/refresh")) {
        return new Response(JSON.stringify({ access_token: "new", refresh_token: "new" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      sseCalled += 1;
      if (sseCalled === 1) {
        // 首次 401
        return new Response(JSON.stringify({ detail: "unauthorized" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        });
      }
      // 重试：返回正常 SSE 流
      return new Response(goodStream, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });
    globalThis.fetch = fn as unknown as typeof fetch;

    const events: SSEEvent[] = [];
    await streamSSE("/agents/x/execute/stream", { input: "hi" }, (e) => events.push(e));
    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("done");
    // 3 次 fetch：SSE 401 + refresh + SSE 重试 200
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("初始 401 + refresh 失败 → 触发 unauthorized handler 并抛 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const fn = vi.fn(async (url: string) => {
      if (url.includes("/auth/refresh")) {
        return new Response(JSON.stringify({ detail: "invalid" }), {
          status: 401,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ detail: "unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fn as unknown as typeof fetch;

    await expect(
      streamSSE("/agents/x/execute/stream", { input: "hi" }, () => {}),
    ).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
