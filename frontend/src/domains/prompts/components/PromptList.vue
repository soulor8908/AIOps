<script setup lang="ts">
import { onMounted, ref } from "vue";
import { usePromptStore } from "../store";
import { Button, Input, Badge, Alert, Skeleton } from "@/shared/ui";
import { formatDate } from "@/shared/utils";
import PromptEditor from "./PromptEditor.vue";

const store = usePromptStore();
const searchQuery = ref("");
const showEditor = ref(false);

function onSearch() {
  store.fetchList(searchQuery.value);
}

function onCreated() {
  showEditor.value = false;
  store.fetchList();
}

onMounted(() => store.fetchList());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center gap-2">
      <Input
        v-model="searchQuery"
        placeholder="Search prompts..."
        class="max-w-sm"
        @keyup.enter="onSearch"
      />
      <Button @click="onSearch">Search</Button>
      <Button variant="outline" @click="showEditor = !showEditor">
        {{ showEditor ? "Cancel" : "+ New" }}
      </Button>
    </div>

    <PromptEditor v-if="showEditor" @created="onCreated" />

    <Alert v-if="store.error" :message="store.error" @retry="store.fetchList(searchQuery)" />

    <div v-if="store.loading" class="space-y-2">
      <Skeleton v-for="i in 4" :key="i" class="h-16 w-full" />
    </div>

    <div
      v-else-if="!store.error && store.items.length === 0"
      class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground"
    >
      No prompts yet. Click "+ New" to create one.
    </div>

    <div v-else class="overflow-hidden rounded-md border">
      <div
        v-for="prompt in store.items"
        :key="prompt.id"
        class="flex cursor-pointer items-center justify-between border-b p-4 transition-colors last:border-b-0 hover:bg-muted/50"
        :class="{ 'bg-muted': store.selectedId === prompt.id }"
        @click="store.select(prompt.id)"
      >
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="font-medium">{{ prompt.name }}</span>
            <Badge variant="secondary">{{ prompt.versions.length }} versions</Badge>
          </div>
          <div class="truncate text-sm text-muted-foreground">
            {{ prompt.description || "No description" }}
          </div>
        </div>
        <div class="ml-4 shrink-0 text-xs text-muted-foreground">
          {{ formatDate(prompt.updated_at) }}
        </div>
      </div>
    </div>
  </div>
</template>
