import { defineStore } from "pinia";
import { ref } from "vue";

export type ToastVariant = "error" | "warning" | "info" | "success";

export interface Toast {
  id: number;
  message: string;
  variant: ToastVariant;
  // 自动过期毫秒数，0 表示不自动过期
  timeout: number;
}

let _nextId = 1;

export const useToastStore = defineStore("toast", () => {
  const toasts = ref<Toast[]>([]);

  /**
   * 推送一条 toast。默认 5s 自动移除（error 变体 8s，给用户更多阅读时间）。
   * @returns toast id（可用于手动移除）
   */
  function pushToast(
    message: string,
    variant: ToastVariant = "info",
    timeout?: number,
  ): number {
    const id = _nextId++;
    const autoTimeout =
      timeout ?? (variant === "error" ? 8_000 : 5_000);
    toasts.value.push({ id, message, variant, timeout: autoTimeout });
    if (autoTimeout > 0) {
      setTimeout(() => removeToast(id), autoTimeout);
    }
    return id;
  }

  function removeToast(id: number): void {
    const idx = toasts.value.findIndex((t) => t.id === id);
    if (idx >= 0) toasts.value.splice(idx, 1);
  }

  function clearAll(): void {
    toasts.value = [];
  }

  return { toasts, pushToast, removeToast, clearAll };
});
