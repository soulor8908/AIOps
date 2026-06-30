import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "dashboard",
    component: () => import("./views/DashboardView.vue"),
    meta: { title: "Dashboard" },
  },
  {
    path: "/prompts",
    name: "prompts",
    component: () => import("./views/PromptsView.vue"),
    meta: { title: "Prompt Studio" },
  },
  {
    path: "/agents",
    name: "agents",
    component: () => import("./views/AgentsView.vue"),
    meta: { title: "Agent Orchestrator" },
  },
  {
    path: "/knowledge",
    name: "knowledge",
    component: () => import("./views/KnowledgeView.vue"),
    meta: { title: "Knowledge Base" },
  },
  {
    path: "/models",
    name: "models",
    component: () => import("./views/ModelsView.vue"),
    meta: { title: "Model Router" },
  },
  {
    path: "/analytics",
    name: "analytics",
    component: () => import("./views/AnalyticsView.vue"),
    meta: { title: "Conversation Analytics" },
  },
  {
    path: "/evals",
    name: "evals",
    component: () => import("./views/EvalsView.vue"),
    meta: { title: "Eval Suite" },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? "AIOps Console";
  document.title = `${title} - AIOps Console`;
});
