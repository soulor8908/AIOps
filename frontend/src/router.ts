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

// P3-UX-H3：路由守卫。受保护页未登录 → 跳 /login（带 redirect 参数）；
// 已登录访问 /login → 跳首页。首次进入受保护页时补拉 /auth/me 校验 token
// 有效性（token 过期则 fetchMe 内部 logout，随后 requiresAuth 判定失败跳登录）。
router.beforeEach(async (to) => {
  const userStore = useUserStore();

  if (to.name === "login" && userStore.isAuthenticated) {
    return { name: "dashboard" };
  }

  if (
    to.meta.requiresAuth &&
    userStore.isAuthenticated &&
    !userStore.user
  ) {
    await userStore.fetchMe();
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
