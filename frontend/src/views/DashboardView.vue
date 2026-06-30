<script setup lang="ts">
import Dashboard from "@/domains/analytics/components/Dashboard.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Dashboard</h1>
      <p class="text-sm text-muted-foreground">
        Overview of conversations, token usage, and cost across the platform.
      </p>
    </div>
    <Dashboard />
  </div>
</template>
