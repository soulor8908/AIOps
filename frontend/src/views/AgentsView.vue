<script setup lang="ts">
import AgentList from "@/domains/agents/components/AgentList.vue";
import AgentDetail from "@/domains/agents/components/AgentDetail.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Agent Orchestrator</h1>
      <p class="text-sm text-muted-foreground">
        Define agents, attach tools, and execute them with trace visibility.
      </p>
    </div>
    <div class="grid gap-6 lg:grid-cols-2">
      <AgentList />
      <AgentDetail />
    </div>
  </div>
</template>
