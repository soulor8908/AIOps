<script setup lang="ts">
import { ref } from "vue";
import { useKnowledgeStore } from "../store";
import { Alert, Button, Input, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatPercent } from "@/shared/utils";

const store = useKnowledgeStore();
const query = ref("");
const topK = ref("5");
const fileInput = ref<HTMLInputElement | null>(null);

async function onSearch() {
  if (!query.value.trim()) return;
  const k = Number(topK.value) || 5;
  await store.search(query.value, k);
}

function onUpload() {
  fileInput.value?.click();
}

async function onFileChange(e: Event) {
  const target = e.target as HTMLInputElement;
  const file = target.files?.[0];
  if (file) await store.uploadDocument(file);
  target.value = "";
}
</script>

<template>
  <div v-if="!store.selected" class="flex h-full items-center justify-center text-sm text-muted-foreground">
    Select a knowledge base to search and upload documents.
  </div>

  <div v-else class="space-y-4">
    <Alert v-if="store.error" :message="store.error" />

    <Card>
      <CardHeader>
        <div class="flex items-center justify-between">
          <CardTitle>{{ store.selected.name }}</CardTitle>
          <Button size="sm" variant="outline" @click="onUpload">
            {{ store.uploading ? "Uploading..." : "Upload Document" }}
          </Button>
          <input
            ref="fileInput"
            type="file"
            class="hidden"
            accept=".txt,.md,.pdf,.docx"
            @change="onFileChange"
          />
        </div>
      </CardHeader>
      <CardContent>
        <p class="text-sm text-muted-foreground">{{ store.selected.description }}</p>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>Semantic Search</CardTitle></CardHeader>
      <CardContent>
        <div class="flex items-center gap-2">
          <Input v-model="query" placeholder="Ask a question..." class="flex-1" @keyup.enter="onSearch" />
          <Input v-model="topK" type="number" class="w-20" />
          <Button :disabled="store.searching" @click="onSearch">
            {{ store.searching ? "Searching..." : "Search" }}
          </Button>
        </div>

        <div v-if="store.searchResults.length" class="mt-4 space-y-3">
          <div
            v-for="r in store.searchResults"
            :key="r.chunk_id"
            class="rounded-md border p-3"
          >
            <div class="mb-1 flex items-center justify-between">
              <Badge variant="outline">doc #{{ r.document_id }}</Badge>
              <span class="text-xs text-muted-foreground">
                score: {{ formatPercent(r.score) }}
              </span>
            </div>
            <p class="text-sm">{{ r.content }}</p>
          </div>
        </div>
        <div v-else-if="!store.searching" class="mt-4 text-center text-sm text-muted-foreground">
          No results yet. Run a search to see matched chunks.
        </div>
      </CardContent>
    </Card>
  </div>
</template>
