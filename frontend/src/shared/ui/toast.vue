<script setup lang="ts">
import { cn } from "@/shared/utils";
import type { ToastVariant } from "@/shared/stores/toast";

const props = defineProps<{
  variant: ToastVariant;
  message: string;
}>();

const emit = defineEmits<{ close: [] }>();

const variantClasses: Record<ToastVariant, string> = {
  error: "border-destructive/50 bg-destructive/10 text-destructive",
  warning: "border-yellow-500/50 bg-yellow-500/10 text-yellow-700 dark:text-yellow-400",
  info: "border-blue-500/50 bg-blue-500/10 text-blue-700 dark:text-blue-400",
  success: "border-green-500/50 bg-green-500/10 text-green-700 dark:text-green-400",
};
</script>

<template>
  <div
    :class="cn(
      'pointer-events-auto flex items-start justify-between gap-3 rounded-md border px-4 py-3 text-sm shadow-md',
      variantClasses[props.variant],
    )"
    role="alert"
  >
    <span>{{ message }}</span>
    <button
      class="shrink-0 rounded text-xs font-medium underline hover:no-underline"
      aria-label="关闭"
      @click="emit('close')"
    >
      ✕
    </button>
  </div>
</template>
