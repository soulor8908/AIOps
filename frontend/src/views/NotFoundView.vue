<script setup lang="ts">
import { RouterLink } from "vue-router";
</script>

<template>
  <div class="flex min-h-screen flex-col items-center justify-center gap-4 bg-background p-4 text-center">
    <div class="text-6xl font-bold text-muted-foreground">404</div>
    <h1 class="text-xl font-semibold">Page not found</h1>
    <p class="max-w-sm text-sm text-muted-foreground">
      The page you are looking for doesn't exist or has been moved.
    </p>
    <RouterLink
      to="/"
      class="inline-flex h-9 items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
    >
      Back to Dashboard
    </RouterLink>
  </div>
</template>
