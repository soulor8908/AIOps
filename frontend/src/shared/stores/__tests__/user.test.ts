import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useUserStore } from "../user";

// Mock api client：拦截 api.get/postForm/post，避免真实 fetch。
// Batch 6c：cookie 模式——不再 mock setToken（已移除），新增 post mock 给 /auth/logout。
vi.mock("@/shared/api/client", () => ({
  api: {
    get: vi.fn(),
    postForm: vi.fn(),
    post: vi.fn(),
  },
  setUnauthorizedHandler: vi.fn(),
}));

import { api } from "@/shared/api/client";

const mockedApi = vi.mocked(api);

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useUserStore — cookie 模式", () => {
  it("初始：未认证、无用户、无 loading/error", () => {
    const store = useUserStore();
    expect(store.isAuthenticated).toBe(false);
    expect(store.user).toBeNull();
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });

  it("login 成功：postForm + fetchMe 后 user 设置、isAuthenticated=true", async () => {
    mockedApi.postForm.mockResolvedValueOnce({
      access_token: "acc-123",
      refresh_token: "ref-456",
      token_type: "bearer",
      expires_in: 3600,
    });
    mockedApi.get.mockResolvedValueOnce({
      id: "u1",
      email: "a@b.com",
      username: "alice",
      role: "user",
      is_active: true,
      created_at: "",
    });

    const store = useUserStore();
    await store.login("a@b.com", "pw");

    // Batch 6c：cookie 模式不存 token，isAuthenticated 由 user 决定
    expect(store.isAuthenticated).toBe(true);
    expect(store.user).toEqual({
      id: "u1",
      email: "a@b.com",
      username: "alice",
      role: "user",
    });
    expect(mockedApi.postForm).toHaveBeenCalledWith("/auth/token", {
      username: "a@b.com",
      password: "pw",
    });
    expect(mockedApi.get).toHaveBeenCalledWith("/auth/me");
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });

  it("login 失败（postForm 抛错）：error 设置、未认证、异常向上抛", async () => {
    mockedApi.postForm.mockRejectedValueOnce(new Error("invalid credentials"));
    const store = useUserStore();
    await expect(store.login("a@b.com", "wrong")).rejects.toThrow(
      "invalid credentials",
    );
    expect(store.error).toBe("invalid credentials");
    expect(store.isAuthenticated).toBe(false);
    expect(store.user).toBeNull();
    expect(store.loading).toBe(false);
  });

  it("login 成功但 fetchMe 失败 → logout 清理后向上抛", async () => {
    mockedApi.postForm.mockResolvedValueOnce({
      access_token: "acc",
      refresh_token: "ref",
      token_type: "bearer",
      expires_in: 3600,
    });
    mockedApi.get.mockRejectedValueOnce(new Error("me failed"));
    // logout 内部会调 /auth/logout
    mockedApi.post.mockResolvedValueOnce(undefined);

    const store = useUserStore();
    await expect(store.login("a@b.com", "pw")).rejects.toThrow("me failed");
    expect(store.isAuthenticated).toBe(false);
    expect(store.user).toBeNull();
    // logout 已调用 /auth/logout 清 cookie
    expect(mockedApi.post).toHaveBeenCalledWith("/auth/logout", {});
  });

  it("logout：调 /auth/logout + 清空 user + isAuthenticated=false", async () => {
    mockedApi.post.mockResolvedValueOnce(undefined);
    const store = useUserStore();
    // 模拟已登录态
    store.user = {
      id: "u1",
      email: "a@b.com",
      username: "alice",
      role: "user",
    };
    await store.logout();
    expect(mockedApi.post).toHaveBeenCalledWith("/auth/logout", {});
    expect(store.user).toBeNull();
    expect(store.isAuthenticated).toBe(false);
    expect(store.error).toBeNull();
  });

  it("logout 网络失败也清空本地状态", async () => {
    mockedApi.post.mockRejectedValueOnce(new Error("network"));
    const store = useUserStore();
    store.user = {
      id: "u1",
      email: "a@b.com",
      username: "alice",
      role: "user",
    };
    await store.logout();
    // 即使 /auth/logout 失败，本地状态也清空
    expect(store.user).toBeNull();
    expect(store.isAuthenticated).toBe(false);
  });

  it("fetchMe：成功后 user 设置", async () => {
    mockedApi.get.mockResolvedValueOnce({
      id: "u2",
      email: "b@c.com",
      username: "bob",
      role: "admin",
      is_active: true,
      created_at: "",
    });
    const store = useUserStore();
    await store.fetchMe();
    expect(store.user).toEqual({
      id: "u2",
      email: "b@c.com",
      username: "bob",
      role: "admin",
    });
    expect(store.isAuthenticated).toBe(true);
  });

  it("fetchMe：失败时抛出（调用方处理）", async () => {
    mockedApi.get.mockRejectedValueOnce(new Error("401"));
    const store = useUserStore();
    await expect(store.fetchMe()).rejects.toThrow("401");
    expect(store.user).toBeNull();
    expect(store.isAuthenticated).toBe(false);
  });
});
