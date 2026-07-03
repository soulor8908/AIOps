<script setup lang="ts">
import { cn } from "@/shared/utils";

withDefaults(
  defineProps<{
    class?: string;
  }>(),
  { class: "" },
);
</script>

<template>
  <div :class="cn('animate-pulse rounded-md bg-muted', $props.class)" aria-hidden="true" />
</template>
