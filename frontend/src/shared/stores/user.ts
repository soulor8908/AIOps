import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, setToken } from "@/shared/api/client";
import type { Token, UserOut } from "@/shared/api/types";

export interface UserInfo {
  id: string;
  email: string;
  username: string;
  role: "admin" | "user";
}

/** 将后端 UserOut 映射为前端 store 内部 UserInfo。 */
function toUserInfo(u: UserOut): UserInfo {
  return {
    id: u.id,
    email: u.email,
    username: u.username,
    role: u.role,
  };
}

export const useUserStore = defineStore("user", () => {
  const token = ref<string>(localStorage.getItem("token") || "");
  const refreshToken = ref<string>(localStorage.getItem("refresh_token") || "");
  const user = ref<UserInfo | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => Boolean(token.value));

  // 后端登录端点为 OAuth2PasswordRequestForm（/auth/token），username 字段即邮箱。
  async function login(email: string, password: string): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.postForm<Token>("/auth/token", {
        username: email,
        password,
      });
      token.value = res.access_token;
      refreshToken.value = res.refresh_token;
      setToken(res.access_token);
      if (res.refresh_token) {
        localStorage.setItem("refresh_token", res.refresh_token);
      }
      // Token 响应不含用户信息，需额外拉取 /auth/me。
      // P1：fetchMe 失败时必须抛出，否则 login() resolve 成功 → 路由守卫
      // 发现 !isAuthenticated 跳回 /login，形成登录死循环且无错误提示。
      try {
        await fetchMe();
      } catch (e) {
        logout();
        throw e;
      }
      // fetchMe 成功但 user 仍为空（理论不应发生），防御性抛错
      if (!user.value) {
        logout();
        throw new Error("登录后未能获取用户信息");
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Login failed";
      throw e;
    } finally {
      loading.value = false;
    }
  }

  async function fetchMe(): Promise<void> {
    if (!token.value) return;
    // P1：失败时抛出而非静默 logout，让调用方（login / 路由守卫）感知并处理。
    // 路由守卫的 try/catch 会捕获并保持未认证态跳转登录页。
    const me = await api.get<UserOut>("/auth/me");
    user.value = toUserInfo(me);
  }

  function logout(): void {
    token.value = "";
    refreshToken.value = "";
    user.value = null;
    // P2：登出时清空 error，避免残留上次登录失败信息在重新进入登录页时仍显示。
    error.value = null;
    setToken("");
    localStorage.removeItem("refresh_token");
  }

  // P3：多标签页 token 同步。当另一个标签页登出/换 token 时，
  // 本标签页内存中的 token 仍是旧值，会持续 401。监听 storage 事件同步状态。
  if (typeof window !== "undefined") {
    window.addEventListener("storage", (e: StorageEvent) => {
      if (e.key !== "token") return;
      const newToken = e.newValue || "";
      if (newToken === token.value) return;
      token.value = newToken;
      setToken(newToken);
      if (!newToken) {
        // 另一标签页登出 → 同步登出本标签页（不清 localStorage，源头已清）
        user.value = null;
        refreshToken.value = "";
      }
    });
  }

  return {
    token,
    refreshToken,
    user,
    loading,
    error,
    isAuthenticated,
    login,
    fetchMe,
    logout,
  };
});
