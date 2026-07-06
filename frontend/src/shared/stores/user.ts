import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, setUnauthorizedHandler } from "@/shared/api/client";
import type { UserOut } from "@/shared/api/types";

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

/**
 * Batch 6c：多标签页会话同步频道。
 *
 * cookie 模式下不再有 localStorage storage 事件可监听。改用 BroadcastChannel
 * 广播登出/登录事件：一个标签页登出时，其它标签页同步清空 user 状态，避免
 * 残留已失效的会话继续操作。
 */
const CHANNEL_NAME = "aiops-auth";
const MSG_LOGOUT = "logout";

function createAuthChannel(): BroadcastChannel | null {
  if (typeof window === "undefined") return null;
  if (typeof BroadcastChannel === "undefined") return null;
  return new BroadcastChannel(CHANNEL_NAME);
}

export const useUserStore = defineStore("user", () => {
  // Batch 6c：token 不再存前端（httpOnly cookie 由浏览器管理）。会话状态以
  // ``user`` 是否为 null 为准——login/fetchMe 成功后 set，logout/401 失败后 clear。
  const user = ref<UserInfo | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => user.value !== null);

  const channel = createAuthChannel();
  if (channel) {
    channel.addEventListener("message", (e: MessageEvent) => {
      if (e.data === MSG_LOGOUT) {
        // 另一标签页登出 → 同步清空本标签页会话（不再调 /auth/logout，源头已调）
        user.value = null;
        error.value = null;
      }
    });
  }

  /**
   * Batch 6c：清空本地会话状态（不调 /auth/logout）。
   *
   * 用于 unauthorized handler（401 + refresh 失败）与多标签页同步——此时
   * httpOnly cookie 已无效/即将失效，仅需清前端状态让 UI 反映登出。
   * cookie 的实际清除由 /auth/logout 端点完成（显式登出时调）或下次登录覆盖。
   */
  function _clearSession(): void {
    user.value = null;
    error.value = null;
    channel?.postMessage(MSG_LOGOUT);
  }

  // Batch 6c：注册 unauthorized handler——client 在 401 + refresh 失败时触发，
  // 清空本地会话状态。路由守卫/App.vue watch 会据此跳登录页。
  setUnauthorizedHandler(() => {
    _clearSession();
  });

  // 后端登录端点为 OAuth2PasswordRequestForm（/auth/token），username 字段即邮箱。
  async function login(email: string, password: string): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      // /auth/token 成功后服务端 set httpOnly cookie，前端无需感知 token 明文。
      await api.postForm("/auth/token", { username: email, password });
      // cookie 已下发，拉取用户信息确认会话建立成功。
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
    // Batch 6c：cookie 模式下无本地 token 标志，直接调 /auth/me。
    // 401 由 client 层 refresh 重试处理；refresh 也失败则 unauthorized handler
    // 清空状态，fetchMe 抛 401 让调用方（login/router）感知。
    const me = await api.get<UserOut>("/auth/me");
    user.value = toUserInfo(me);
  }

  /**
   * Batch 6c：登出——调 /auth/logout 清除 httpOnly cookie + 撤销 token，
   * 然后清空本地状态并广播给其它标签页。
   *
   * /auth/logout 不要求有效 token（access token 可能已过期），尽力撤销 + 清 cookie。
   * 网络失败也清本地状态（确保 UI 一致），cookie 残留会在下次登录时被覆盖。
   */
  async function logout(): Promise<void> {
    try {
      await api.post("/auth/logout", {});
    } catch {
      // 网络错误等——仍清本地状态，确保 UI 一致
    }
    _clearSession();
  }

  return {
    user,
    loading,
    error,
    isAuthenticated,
    login,
    fetchMe,
    logout,
  };
});
