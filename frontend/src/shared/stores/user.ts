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
      await fetchMe();
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Login failed";
      throw e;
    } finally {
      loading.value = false;
    }
  }

  async function fetchMe(): Promise<void> {
    if (!token.value) return;
    try {
      const me = await api.get<UserOut>("/auth/me");
      user.value = toUserInfo(me);
    } catch {
      await logout();
    }
  }

  function logout(): void {
    token.value = "";
    refreshToken.value = "";
    user.value = null;
    setToken("");
    localStorage.removeItem("refresh_token");
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
