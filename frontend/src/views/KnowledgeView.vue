<script setup lang="ts">
import KnowledgeBaseList from "@/domains/knowledge/components/KnowledgeBaseList.vue";
import SearchPanel from "@/domains/knowledge/components/SearchPanel.vue";
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">Knowledge Base</h1>
      <p class="text-sm text-muted-foreground">
        Manage RAG knowledge bases, upload documents, and run semantic search.
      </p>
    </div>
    <div class="grid gap-6 lg:grid-cols-2">
      <KnowledgeBaseList />
      <SearchPanel />
    </div>
  </div>
</template>
