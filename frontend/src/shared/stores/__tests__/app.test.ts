import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useAppStore } from "../app";

beforeEach(() => {
  // 每个 case 独立 Pinia 实例，避免 store 状态串扰。
  setActivePinia(createPinia());
  document.documentElement.classList.remove("dark");
});

describe("useAppStore", () => {
  it("初始：侧边栏展开、主题 light", () => {
    const store = useAppStore();
    expect(store.sidebarCollapsed).toBe(false);
    expect(store.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("toggleSidebar 切换折叠状态", () => {
    const store = useAppStore();
    store.toggleSidebar();
    expect(store.sidebarCollapsed).toBe(true);
    store.toggleSidebar();
    expect(store.sidebarCollapsed).toBe(false);
  });

  it("toggleTheme 在 light/dark 间切换并同步 document class", () => {
    const store = useAppStore();
    store.toggleTheme();
    expect(store.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    store.toggleTheme();
    expect(store.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("setTheme 显式设置主题并同步 document class", () => {
    const store = useAppStore();
    store.setTheme("dark");
    expect(store.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    // 再设为 light 应移除 class
    store.setTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
