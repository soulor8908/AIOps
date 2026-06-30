<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
import { useAppStore } from "@/shared/stores/app";

const appStore = useAppStore();

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
</script>

<template>
  <div class="flex h-screen overflow-hidden bg-background">
    <aside
      class="flex flex-col border-r bg-card transition-all duration-200"
      :class="appStore.sidebarCollapsed ? 'w-16' : 'w-64'"
    >
      <div class="flex h-14 items-center gap-2 border-b px-4">
        <div
          class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground"
        >
          AI
        </div>
        <span v-if="!appStore.sidebarCollapsed" class="text-sm font-semibold">
          AIOps Console
        </span>
      </div>

      <nav class="flex-1 space-y-1 overflow-y-auto p-2">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          active-class="bg-accent text-accent-foreground"
          :title="item.label"
        >
          <span
            class="flex h-5 w-5 shrink-0 items-center justify-center rounded text-xs font-bold"
          >
            {{ item.icon }}
          </span>
          <span v-if="!appStore.sidebarCollapsed">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <button
        class="flex items-center gap-3 border-t px-3 py-2 text-sm text-muted-foreground hover:bg-accent"
        @click="appStore.toggleSidebar"
      >
        <span class="flex h-5 w-5 items-center justify-center">
          {{ appStore.sidebarCollapsed ? ">" : "<" }}
        </span>
        <span v-if="!appStore.sidebarCollapsed">Collapse</span>
      </button>
    </aside>

    <div class="flex flex-1 flex-col overflow-hidden">
      <header
        class="flex h-14 shrink-0 items-center justify-between border-b bg-card px-6"
      >
        <h1 class="text-base font-semibold">AIOps Console</h1>
        <div class="text-sm text-muted-foreground">
          v0.1.0-alpha
        </div>
      </header>

      <main class="flex-1 overflow-y-auto p-6">
        <RouterView />
      </main>
    </div>
  </div>
</template>
