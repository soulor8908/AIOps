<script setup lang="ts">
import PromptList from "@/domains/prompts/components/PromptList.vue";
import PromptDetail from "@/domains/prompts/components/PromptDetail.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Prompt Studio</h1>
      <p class="text-sm text-muted-foreground">
        Versioned prompt management with rollback and diffing.
      </p>
    </div>
    <div class="grid gap-6 lg:grid-cols-2">
      <PromptList />
      <PromptDetail />
    </div>
  </div>
</template>
