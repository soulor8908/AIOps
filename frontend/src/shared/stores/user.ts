import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, setToken } from "@/shared/api/client";

export interface UserInfo {
  id: number;
  email: string;
  role: string;
}

export const useUserStore = defineStore("user", () => {
  const token = ref<string>(localStorage.getItem("token") || "");
  const user = ref<UserInfo | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => Boolean(token.value));

  async function login(email: string, password: string): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.post<{ access_token: string; user: UserInfo }>(
        "/auth/login",
        { email, password },
      );
      token.value = res.access_token;
      user.value = res.user;
      setToken(res.access_token);
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
      user.value = await api.get<UserInfo>("/auth/me");
    } catch {
      await logout();
    }
  }

  function logout(): void {
    token.value = "";
    user.value = null;
    setToken("");
  }

  return { token, user, loading, error, isAuthenticated, login, fetchMe, logout };
});
