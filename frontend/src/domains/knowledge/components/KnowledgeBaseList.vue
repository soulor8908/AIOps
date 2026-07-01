<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useKnowledgeStore } from "../store";
import { Button, Input } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate } from "@/shared/utils";
import type { KnowledgeBaseCreate } from "@/shared/api/types";

const store = useKnowledgeStore();
const showForm = ref(false);

const form = ref({
  name: "",
  description: "",
  embedding_model: "text-embedding-3-small",
  chunk_size: "512",
  chunk_overlap: "50",
});

function buildPayload(): KnowledgeBaseCreate {
  return {
    name: form.value.name,
    description: form.value.description,
    embedding_model: form.value.embedding_model,
    chunk_size: Number(form.value.chunk_size) || 512,
    chunk_overlap: Number(form.value.chunk_overlap) || 50,
  };
}

async function onCreate() {
  await store.create(buildPayload());
  form.value = {
    name: "",
    description: "",
    embedding_model: "text-embedding-3-small",
    chunk_size: "512",
    chunk_overlap: "50",
  };
  showForm.value = false;
}

onMounted(() => store.fetchList());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">Knowledge Bases</h2>
      <Button variant="outline" @click="showForm = !showForm">
        {{ showForm ? "Cancel" : "+ New KB" }}
      </Button>
    </div>

    <Card v-if="showForm">
      <CardHeader><CardTitle>Create Knowledge Base</CardTitle></CardHeader>
      <CardContent>
        <form class="grid grid-cols-2 gap-3" @submit.prevent="onCreate">
          <div class="col-span-2 space-y-1">
            <label class="text-sm font-medium">Name</label>
            <Input v-model="form.name" placeholder="kb-name" />
          </div>
          <div class="col-span-2 space-y-1">
            <label class="text-sm font-medium">Description</label>
            <Input v-model="form.description" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Embedding Model</label>
            <Input v-model="form.embedding_model" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Chunk Size</label>
            <Input v-model="form.chunk_size" type="number" />
          </div>
          <Button type="submit" class="col-span-2">Create</Button>
        </form>
      </CardContent>
    </Card>

    <div v-if="store.loading" class="text-sm text-muted-foreground">Loading...</div>
    <div v-else-if="store.knowledgeBases.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
      No knowledge bases yet.
    </div>
    <div v-else class="grid gap-3 sm:grid-cols-2">
      <Card
        v-for="kb in store.knowledgeBases"
        :key="kb.id"
        class="cursor-pointer transition-colors hover:bg-muted/50"
        :class="{ 'ring-2 ring-ring': store.selectedId === kb.id }"
        @click="store.select(kb.id)"
      >
        <CardHeader>
          <div class="flex items-center justify-between">
            <CardTitle>{{ kb.name }}</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <p class="text-sm text-muted-foreground">{{ kb.description || "No description" }}</p>
          <div class="mt-2 flex flex-wrap gap-2 text-xs text-muted-foreground">
            <span>embed: {{ kb.embedding_model }}</span>
            <span>chunk: {{ kb.chunk_size }}</span>
            <span>{{ formatDate(kb.created_at) }}</span>
          </div>
        </CardContent>
      </Card>
    </div>
  </div>
</template>
