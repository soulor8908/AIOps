<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAgentStore } from "../store";
import { Button, Input, Badge, Alert, Skeleton } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate } from "@/shared/utils";
import type { AgentCreate } from "@/shared/api/types";

const store = useAgentStore();
const showForm = ref(false);

const form = ref<AgentCreate>({
  name: "",
  description: "",
  system_prompt: "",
  model_alias: "",
  tools: [],
  max_turns: 10,
  temperature: 0.7,
});
const toolsInput = ref("");

async function onCreate() {
  form.value.tools = toolsInput.value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((name) => ({ name, type: "custom" as const, config: {} }));
  try {
    await store.create(form.value);
  } catch {
    // error 已写入 store.error，保留表单内容供用户修正后重试
    return;
  }
  form.value = { name: "", description: "", system_prompt: "", model_alias: "", tools: [], max_turns: 10, temperature: 0.7 };
  toolsInput.value = "";
  showForm.value = false;
}

onMounted(() => store.fetchAgents());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">Agents</h2>
      <Button variant="outline" @click="showForm = !showForm">
        {{ showForm ? "Cancel" : "+ New Agent" }}
      </Button>
    </div>

    <Card v-if="showForm">
      <CardHeader><CardTitle>Create Agent</CardTitle></CardHeader>
      <CardContent>
        <form class="space-y-3" @submit.prevent="onCreate">
          <div class="space-y-1">
            <label class="text-sm font-medium">Name</label>
            <Input v-model="form.name" placeholder="agent-name" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Model Alias</label>
            <Input v-model="form.model_alias" placeholder="gpt-4o-mini" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Tools (comma-separated)</label>
            <Input v-model="toolsInput" placeholder="search, calculator" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">System Prompt</label>
            <textarea
              v-model="form.system_prompt"
              rows="4"
              class="w-full rounded-md border border-input bg-transparent p-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
          <Button type="submit">Create</Button>
        </form>
      </CardContent>
    </Card>

    <Alert v-if="store.error" :message="store.error" @retry="store.fetchAgents()" />

    <div v-if="store.loading" class="space-y-2">
      <Skeleton v-for="i in 4" :key="i" class="h-16 w-full" />
    </div>

    <div v-else-if="!store.error && store.agents.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
      No agents yet.
    </div>
    <div v-else class="overflow-hidden rounded-md border">
      <div
        v-for="agent in store.agents"
        :key="agent.id"
        class="cursor-pointer border-b p-4 transition-colors last:border-b-0 hover:bg-muted/50"
        :class="{ 'bg-muted': store.selectedId === agent.id }"
        @click="store.select(agent.id)"
      >
        <div class="flex items-center justify-between">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-medium">{{ agent.name }}</span>
              <Badge variant="secondary">{{ agent.model_alias }}</Badge>
            </div>
            <div class="text-sm text-muted-foreground">
              {{ agent.description || "No description" }}
            </div>
            <div v-if="agent.tools.length" class="mt-1 flex flex-wrap gap-1">
              <Badge v-for="(t, idx) in agent.tools" :key="String(t.name ?? idx)" variant="outline">{{ t.name ?? t }}</Badge>
            </div>
          </div>
          <div class="text-xs text-muted-foreground">{{ formatDate(agent.created_at) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
