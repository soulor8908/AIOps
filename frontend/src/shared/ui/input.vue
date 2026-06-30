<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/shared/utils";

const props = withDefaults(
  defineProps<{
    modelValue?: string | number;
    type?: string;
    placeholder?: string;
    disabled?: boolean;
    modelModifiers?: Record<string, boolean>;
  }>(),
  {
    modelValue: "",
    type: "text",
    placeholder: "",
    disabled: false,
    modelModifiers: () => ({}),
  },
);

const emit = defineEmits<{
  "update:modelValue": [value: string | number];
}>();

const value = computed({
  get: () => props.modelValue,
  set: (v: string) => {
    // Support `v-model.number` for numeric bindings.
    const next: string | number = props.modelModifiers?.number
      ? Number(v)
      : v;
    emit("update:modelValue", next);
  },
});
</script>

<template>
  <input
    v-model="value"
    :type="type"
    :placeholder="placeholder"
    :disabled="disabled"
    :class="
      cn(
        'flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors',
        'placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
        'disabled:cursor-not-allowed disabled:opacity-50',
      )
    "
  />
</template>
