<script setup lang="ts">
import ConversationList from "@/domains/analytics/components/ConversationList.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Conversation Analytics</h1>
      <p class="text-sm text-muted-foreground">
        Inspect conversation records, token consumption, and quality scores.
      </p>
    </div>
    <ConversationList />
  </div>
</template>
