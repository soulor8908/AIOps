<script setup lang="ts">
import ModelList from "@/domains/models/components/ModelList.vue";
import ChatTester from "@/domains/models/components/ChatTester.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Model Router</h1>
      <p class="text-sm text-muted-foreground">
        Configure model providers, routing strategies, and test chat completions.
      </p>
    </div>
    <div class="grid gap-6 lg:grid-cols-2">
      <ModelList />
      <ChatTester />
    </div>
  </div>
</template>
