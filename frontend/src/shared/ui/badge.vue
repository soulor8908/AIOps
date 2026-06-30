<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/shared/utils";

const props = withDefaults(
  defineProps<{
    variant?: "default" | "secondary" | "destructive" | "outline";
  }>(),
  { variant: "default" },
);

const variantClasses = {
  default: "border-transparent bg-primary text-primary-foreground",
  secondary: "border-transparent bg-secondary text-secondary-foreground",
  destructive:
    "border-transparent bg-destructive text-destructive-foreground",
  outline: "text-foreground",
};

const classes = computed(() =>
  cn(
    "inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors",
    variantClasses[props.variant],
  ),
);
</script>

<template>
  <span :class="classes">
    <slot />
  </span>
</template>
