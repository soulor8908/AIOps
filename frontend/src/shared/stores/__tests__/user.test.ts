import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useUserStore } from "../user";

// Mock api client：拦截 api.get/postForm，避免真实 fetch。
vi.mock("@/shared/api/client", () => ({
  api: {
    get: vi.fn(),
    postForm: vi.fn(),
  },
  setToken: vi.fn(),
}));

import { api, setToken } from "@/shared/api/client";

const mockedApi = vi.mocked(api);
const mockedSetToken = vi.mocked(setToken);

beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useUserStore", () => {
  it("初始：未认证、无用户、无 loading/error", () => {
    const store = useUserStore();
    expect(store.isAuthenticated).toBe(false);
    expect(store.user).toBeNull();
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });

  it("login 成功：存 token、拉 me、setToken、写 localStorage", async () => {
    mockedApi.postForm.mockResolvedValueOnce({
      access_token: "acc-123",
      refresh_token: "ref-456",
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

    expect(store.token).toBe("acc-123");
    expect(store.refreshToken).toBe("ref-456");
    expect(store.isAuthenticated).toBe(true);
    expect(store.user).toEqual({
      id: "u1",
      email: "a@b.com",
      username: "alice",
      role: "user",
    });
    expect(mockedSetToken).toHaveBeenCalledWith("acc-123");
    expect(localStorage.getItem("refresh_token")).toBe("ref-456");
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });

  it("login 失败：error 被设置、token 未存、异常向上抛", async () => {
    mockedApi.postForm.mockRejectedValueOnce(new Error("invalid credentials"));
    const store = useUserStore();
    await expect(store.login("a@b.com", "wrong")).rejects.toThrow(
      "invalid credentials",
    );
    expect(store.error).toBe("invalid credentials");
    expect(store.token).toBe("");
    expect(store.isAuthenticated).toBe(false);
    expect(store.loading).toBe(false);
  });

  it("logout：清空 token/user、setToken('')、移除 localStorage", () => {
    const store = useUserStore();
    localStorage.setItem("refresh_token", "stale");
    store.logout();
    expect(store.token).toBe("");
    expect(store.refreshToken).toBe("");
    expect(store.user).toBeNull();
    expect(store.isAuthenticated).toBe(false);
    expect(mockedSetToken).toHaveBeenCalledWith("");
    expect(localStorage.getItem("refresh_token")).toBeNull();
  });

  it("fetchMe：无 token 时直接返回，不调用 api", async () => {
    const store = useUserStore();
    await store.fetchMe();
    expect(mockedApi.get).not.toHaveBeenCalled();
  });
});
