<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";
import { useAppStore } from "@/shared/stores/app";
import { useUserStore } from "@/shared/stores/user";
import { useToastStore } from "@/shared/stores/toast";
import { Toast } from "@/shared/ui";

const appStore = useAppStore();
const userStore = useUserStore();
const toastStore = useToastStore();
const route = useRoute();
const router = useRouter();

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const navItems: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "M" },
  { to: "/prompts", label: "Prompt Studio", icon: "P" },
  { to: "/agents", label: "Agent Orchestrator", icon: "A" },
  { to: "/knowledge", label: "Knowledge Base", icon: "K" },
  { to: "/models", label: "Model Router", icon: "R" },
  { to: "/analytics", label: "Analytics", icon: "C" },
  { to: "/evals", label: "Eval Suite", icon: "E" },
];

// P3-UX-H3：登录页 / 404 走无侧边栏的 blank 布局，其余走 app 布局。
const showChrome = computed(() => route.meta.public !== true);

// P1-1：登出后主动跳转登录页。vue-router beforeEach 仅在导航时触发，
// 当前路由不变则守卫不执行，用户会停留在受保护页面（后续请求 401 但页面不跳）。
async function onLogout() {
  userStore.logout();
  await router.push({ name: "login" });
}
</script>

<template>
  <RouterView v-if="!showChrome" />

  <div v-else class="flex h-screen overflow-hidden bg-background">
    <aside
      class="flex flex-col border-r bg-card transition-all duration-200"
      :class="appStore.sidebarCollapsed ? 'w-16' : 'w-64'"
    >
      <div class="flex h-14 items-center gap-2 border-b px-4">
        <div
          class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground"
          aria-hidden="true"
        >
          AI
        </div>
        <span v-if="!appStore.sidebarCollapsed" class="text-sm font-semibold">
          AIOps Console
        </span>
      </div>

      <nav class="flex-1 space-y-1 overflow-y-auto p-2" aria-label="Main navigation">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          :active-class="item.to === '/' ? '' : 'bg-accent text-accent-foreground'"
          :exact-active-class="item.to === '/' ? 'bg-accent text-accent-foreground' : ''"
          :title="item.label"
          :aria-label="item.label"
        >
          <span
            class="flex h-5 w-5 shrink-0 items-center justify-center rounded text-xs font-bold"
            aria-hidden="true"
          >
            {{ item.icon }}
          </span>
          <span v-if="!appStore.sidebarCollapsed">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <button
        class="flex items-center gap-3 border-t px-3 py-2 text-sm text-muted-foreground hover:bg-accent"
        :aria-label="appStore.sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
        @click="appStore.toggleSidebar"
      >
        <span class="flex h-5 w-5 items-center justify-center" aria-hidden="true">
          {{ appStore.sidebarCollapsed ? ">" : "<" }}
        </span>
        <span v-if="!appStore.sidebarCollapsed">Collapse</span>
      </button>
    </aside>

    <div class="flex flex-1 flex-col overflow-hidden">
      <header
        class="flex h-14 shrink-0 items-center justify-between border-b bg-card px-6"
      >
        <!-- P3-4：header 用 div 而非 h1，让各 View 的 h1 作为页面主标题（避免单页两个 h1） -->
        <span class="text-base font-semibold">AIOps Console</span>
        <div class="flex items-center gap-4 text-sm text-muted-foreground">
          <span v-if="userStore.user" class="hidden sm:inline">
            {{ userStore.user.email }}
          </span>
          <button
            class="rounded-md px-2 py-1 text-sm hover:bg-accent hover:text-accent-foreground"
            @click="onLogout"
          >
            Sign out
          </button>
        </div>
      </header>

      <!-- P2-15：skip-to-content 链接 + main 可聚焦，键盘用户跳过侧栏导航 -->
      <a
        href="#main-content"
        class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-primary focus:px-3 focus:py-1 focus:text-sm focus:text-primary-foreground"
      >
        Skip to content
      </a>
      <main id="main-content" class="flex-1 overflow-y-auto p-6" tabindex="-1">
        <RouterView />
      </main>
    </div>
  </div>

  <!-- 全局 toast 容器：固定右上角堆叠，两个布局分支都可见 -->
  <div
    v-if="toastStore.toasts.length"
    class="pointer-events-none fixed right-4 top-4 z-[100] flex w-96 max-w-[calc(100vw-2rem)] flex-col gap-2"
    aria-live="polite"
    aria-atomic="true"
  >
    <Toast
      v-for="t in toastStore.toasts"
      :key="t.id"
      :variant="t.variant"
      :message="t.message"
      @close="toastStore.removeToast(t.id)"
    />
  </div>
</template>
