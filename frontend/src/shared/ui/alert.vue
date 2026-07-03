<script setup lang="ts">
import { cn } from "@/shared/utils";

type Variant = "error" | "warning" | "info";

const props = withDefaults(
  defineProps<{
    variant?: Variant;
    message: string;
  }>(),
  { variant: "error" },
);

const emit = defineEmits<{ retry: [] }>();

const variantClasses: Record<Variant, string> = {
  error: "border-destructive/50 bg-destructive/10 text-destructive",
  warning: "border-yellow-500/50 bg-yellow-500/10 text-yellow-700 dark:text-yellow-400",
  info: "border-blue-500/50 bg-blue-500/10 text-blue-700 dark:text-blue-400",
};
</script>

<template>
  <div :class="cn('flex items-center justify-between rounded-md border px-4 py-3 text-sm', variantClasses[props.variant])">
    <span>{{ message }}</span>
    <button
      v-if="variant === 'error'"
      class="ml-4 shrink-0 rounded text-xs font-medium underline hover:no-underline"
      aria-label="Retry"
      @click="emit('retry')"
    >
      Retry
    </button>
  </div>
</template>
