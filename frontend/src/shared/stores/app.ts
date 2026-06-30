import { defineStore } from "pinia";
import { ref } from "vue";

export const useAppStore = defineStore("app", () => {
  const sidebarCollapsed = ref(false);
  const theme = ref<"light" | "dark">("light");

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value;
  }

  function toggleTheme(): void {
    theme.value = theme.value === "light" ? "dark" : "light";
    document.documentElement.classList.toggle("dark", theme.value === "dark");
  }

  function setTheme(next: "light" | "dark"): void {
    theme.value = next;
    document.documentElement.classList.toggle("dark", next === "dark");
  }

  return { sidebarCollapsed, theme, toggleSidebar, toggleTheme, setTheme };
});
