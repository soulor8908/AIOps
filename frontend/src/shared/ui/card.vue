<script lang="ts">
import { defineComponent, h, type PropType } from "vue";
import { cn } from "@/shared/utils";

// Sub-components co-located in this file (shadcn-vue style).
// Import as: `import Card, { CardHeader, CardTitle, ... } from "@/shared/ui/card"`.

function render(tag: string, baseClass: string) {
  return defineComponent({
    name: tag,
    inheritAttrs: false,
    props: { class: { type: String as PropType<string>, default: "" } },
    setup(props, { attrs, slots }) {
      return () =>
        h(tag, { class: cn(baseClass, props.class, attrs.class as string), ...attrs }, slots.default?.());
    },
  });
}

export const CardHeader = render("div", "flex flex-col space-y-1.5 p-6");
export const CardTitle = render("h3", "font-semibold leading-none tracking-tight");
export const CardDescription = render("p", "text-sm text-muted-foreground");
export const CardContent = render("div", "p-6 pt-0");
export const CardFooter = render("div", "flex items-center p-6 pt-0");
</script>

<script setup lang="ts">
// Default export: the Card container itself.
// `cn` is imported in the <script> block above and shared in module scope.
</script>

<template>
  <div
    :class="
      cn(
        'rounded-md border bg-card text-card-foreground shadow-sm',
        $attrs.class as string,
      )
    "
  >
    <slot />
  </div>
</template>
