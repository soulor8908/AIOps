import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";
import { useUserStore } from "@/shared/stores/user";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("./views/LoginView.vue"),
    meta: { title: "Sign in", public: true },
  },
  {
    path: "/",
    name: "dashboard",
    component: () => import("./views/DashboardView.vue"),
    meta: { title: "Dashboard", requiresAuth: true },
  },
  {
    path: "/prompts",
    name: "prompts",
    component: () => import("./views/PromptsView.vue"),
    meta: { title: "Prompt Studio", requiresAuth: true },
  },
  {
    path: "/agents",
    name: "agents",
    component: () => import("./views/AgentsView.vue"),
    meta: { title: "Agent Orchestrator", requiresAuth: true },
  },
  {
    path: "/knowledge",
    name: "knowledge",
    component: () => import("./views/KnowledgeView.vue"),
    meta: { title: "Knowledge Base", requiresAuth: true },
  },
  {
    path: "/models",
    name: "models",
    component: () => import("./views/ModelsView.vue"),
    meta: { title: "Model Router", requiresAuth: true },
  },
  {
    path: "/analytics",
    name: "analytics",
    component: () => import("./views/AnalyticsView.vue"),
    meta: { title: "Conversation Analytics", requiresAuth: true },
  },
  {
    path: "/evals",
    name: "evals",
    component: () => import("./views/EvalsView.vue"),
    meta: { title: "Eval Suite", requiresAuth: true },
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("./views/NotFoundView.vue"),
    meta: { title: "Not Found", public: true },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

// P3-UX-H3 + Batch 6c：路由守卫。受保护页未登录 → 跳 /login（带 redirect 参数）；
// 已登录访问 /login → 跳首页。
//
// Batch 6c：cookie 模式下页面刷新会丢失内存中的 user 状态（isAuthenticated=false），
// 但 httpOnly cookie 可能仍有效。故首次导航时主动调一次 /auth/me 探测会话：
// - cookie 有效 → fetchMe 成功 set user → isAuthenticated=true
// - cookie 无效 → 401 由 client 层 refresh 重试；refresh 也失败则 unauthorized handler
//   清空状态，fetchMe 抛错，下方 requiresAuth 判定跳登录页
let _bootChecked = false;

router.beforeEach(async (to) => {
  const userStore = useUserStore();

  // 首次导航：尝试从 httpOnly cookie 恢复会话（仅一次，避免每次导航都打 /auth/me）
  if (!_bootChecked) {
    _bootChecked = true;
    if (!userStore.user) {
      try {
        await userStore.fetchMe();
      } catch {
        // 无有效会话——下方 requiresAuth 判定会跳登录页
      }
    }
  }

  if (to.name === "login" && userStore.isAuthenticated) {
    return { name: "dashboard" };
  }

  if (to.meta.requiresAuth && !userStore.isAuthenticated) {
    return { name: "login", query: { redirect: to.fullPath } };
  }

  return true;
});

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? "AIOps Console";
  document.title = `${title} - AIOps Console`;
});
