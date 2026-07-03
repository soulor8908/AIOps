<script setup lang="ts">
import { onUnmounted, watch } from "vue";
import { cn } from "@/shared/utils";

type Variant = "default" | "destructive";

const props = withDefaults(
  defineProps<{
    open: boolean;
    title?: string;
    description?: string;
    confirmText?: string;
    cancelText?: string;
    variant?: Variant;
    loading?: boolean;
  }>(),
  {
    title: "Are you sure?",
    description: "",
    confirmText: "Confirm",
    cancelText: "Cancel",
    variant: "default",
    loading: false,
  },
);

const emit = defineEmits<{
  "update:open": [boolean];
  confirm: [];
  cancel: [];
}>();

function close() {
  emit("update:open", false);
}

function onConfirm() {
  emit("confirm");
}

function onCancel() {
  emit("cancel");
  close();
}

// P3-UX-M2：ESC 关闭 + 打开时锁定背景滚动。
// P1-6：loading 期间禁止 ESC/背景点击关闭，避免提交中途中断。
function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape" && props.open && !props.loading) {
    onCancel();
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      document.addEventListener("keydown", onKeydown);
      document.body.style.overflow = "hidden";
    } else {
      document.removeEventListener("keydown", onKeydown);
      document.body.style.overflow = "";
    }
  },
);

onUnmounted(() => {
  document.removeEventListener("keydown", onKeydown);
  document.body.style.overflow = "";
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center p-4"
    >
      <div
        class="absolute inset-0 bg-black/50"
        :aria-hidden="true"
        @click="!loading && onCancel()"
      />
      <div
        role="alertdialog"
        aria-modal="true"
        :class="cn(
          'relative z-10 w-full max-w-md rounded-lg border bg-card p-6 shadow-lg',
        )"
      >
        <h2 class="text-lg font-semibold">{{ title }}</h2>
        <p v-if="description" class="mt-2 text-sm text-muted-foreground">
          {{ description }}
        </p>
        <div class="mt-6 flex justify-end gap-2">
          <button
            type="button"
            class="inline-flex h-9 items-center justify-center rounded-md border border-input bg-background px-4 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
            :disabled="loading"
            @click="onCancel"
          >
            {{ cancelText }}
          </button>
          <button
            type="button"
            :class="cn(
              'inline-flex h-9 items-center justify-center rounded-md px-4 py-2 text-sm font-medium text-primary-foreground transition-colors disabled:pointer-events-none disabled:opacity-50',
              variant === 'destructive'
                ? 'bg-destructive hover:bg-destructive/90'
                : 'bg-primary hover:bg-primary/90',
            )"
            :disabled="loading"
            @click="onConfirm"
          >
            {{ loading ? "..." : confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
