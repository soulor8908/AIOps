/**
 * 全局错误处理初始化。
 *
 * 注册三个层级的兜底：
 * 1. Vue app.config.errorHandler：组件内未捕获异常（render/setup/event handler）
 * 2. window 'error' 事件：同步运行时错误（如 script 加载失败、eval 错误）
 * 3. window 'unhandledrejection' 事件：未 catch 的 Promise rejection
 *
 * 均推送 toast 提示用户，并 console.error 保留完整堆栈供排障。
 *
 * 必须在 app.mount 前调用，且需在 pinia 初始化后（依赖 toast store）。
 */
import type { App } from "vue";
import { useToastStore } from "./stores/toast";
import { formatApiError } from "./api/errors";

export function setupGlobalErrorHandler(app: App): void {
  // 1. Vue 组件内异常
  app.config.errorHandler = (err, _instance, info) => {
    const message = formatApiError(err);
    console.error("[Vue errorHandler]", err, info);
    try {
      const toastStore = useToastStore();
      toastStore.pushToast(message, "error");
    } catch {
      // pinia 未初始化时静默（main.ts 顺序保证不会到这）
    }
  };

  // 2. window 同步错误
  if (typeof window !== "undefined") {
    window.addEventListener("error", (event) => {
      // 忽略资源加载错误（target 为 Element），它们有专门机制处理
      if (event.target && event.target !== window) return;
      const message = formatApiError(event.error || event.message);
      console.error("[window error]", event.error || event.message);
      try {
        const toastStore = useToastStore();
        toastStore.pushToast(message, "error");
      } catch {
        // pinia 未初始化
      }
    });

    // 3. 未捕获的 Promise rejection
    window.addEventListener("unhandledrejection", (event) => {
      const message = formatApiError(event.reason);
      console.error("[unhandledrejection]", event.reason);
      try {
        const toastStore = useToastStore();
        toastStore.pushToast(message, "error");
      } catch {
        // pinia 未初始化
      }
    });
  }
}
