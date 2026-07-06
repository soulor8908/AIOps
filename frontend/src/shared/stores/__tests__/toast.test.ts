import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useToastStore } from "../toast";

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useToastStore", () => {
  it("pushToast 默认 info 变体 + 5s 自动过期", () => {
    const store = useToastStore();
    const id = store.pushToast("hello");
    expect(store.toasts).toHaveLength(1);
    expect(store.toasts[0]).toMatchObject({
      id,
      message: "hello",
      variant: "info",
    });
    // 5s 后自动移除
    vi.advanceTimersByTime(5_000);
    expect(store.toasts).toHaveLength(0);
  });

  it("pushToast error 变体 8s 自动过期", () => {
    const store = useToastStore();
    store.pushToast("boom", "error");
    expect(store.toasts[0].timeout).toBe(8_000);
    vi.advanceTimersByTime(7_999);
    expect(store.toasts).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(store.toasts).toHaveLength(0);
  });

  it("pushToast timeout=0 不自动过期", () => {
    const store = useToastStore();
    store.pushToast("persistent", "info", 0);
    expect(store.toasts[0].timeout).toBe(0);
    vi.advanceTimersByTime(60_000);
    expect(store.toasts).toHaveLength(1);
  });

  it("pushToast 返回递增 id", () => {
    const store = useToastStore();
    const id1 = store.pushToast("a");
    const id2 = store.pushToast("b");
    expect(id2).toBeGreaterThan(id1);
  });

  it("removeToast 按 id 移除", () => {
    const store = useToastStore();
    const id1 = store.pushToast("a", "info", 0);
    const id2 = store.pushToast("b", "info", 0);
    store.removeToast(id1);
    expect(store.toasts).toHaveLength(1);
    expect(store.toasts[0].id).toBe(id2);
  });

  it("removeToast 不存在的 id 不报错", () => {
    const store = useToastStore();
    store.removeToast(999);
    expect(store.toasts).toHaveLength(0);
  });

  it("clearAll 清空所有", () => {
    const store = useToastStore();
    store.pushToast("a", "info", 0);
    store.pushToast("b", "info", 0);
    store.clearAll();
    expect(store.toasts).toHaveLength(0);
  });

  it("支持 success / warning 变体", () => {
    const store = useToastStore();
    store.pushToast("ok", "success", 0);
    store.pushToast("warn", "warning", 0);
    expect(store.toasts[0].variant).toBe("success");
    expect(store.toasts[1].variant).toBe("warning");
  });
});
