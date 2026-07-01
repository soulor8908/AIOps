<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useModelStore } from "../store";
import { Button, Input, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import type { ModelConfigCreate, ModelProvider } from "@/shared/api/types";

const store = useModelStore();
const showForm = ref(false);

const providers: ModelProvider[] = ["openai", "anthropic", "local", "azure_openai", "custom"];

const form = ref({
  alias: "",
  provider: "openai" as ModelProvider,
  model_name: "",
  temperature: "0.7",
  max_tokens: "4096",
  cost_per_1k_input: "0",
  cost_per_1k_output: "0",
  is_active: true,
  priority: "0",
});

function buildPayload(): ModelConfigCreate {
  return {
    alias: form.value.alias,
    provider: form.value.provider,
    model_name: form.value.model_name,
    temperature: Number(form.value.temperature) || 0.7,
    max_tokens: Number(form.value.max_tokens) || 4096,
    cost_per_1k_input: form.value.cost_per_1k_input,
    cost_per_1k_output: form.value.cost_per_1k_output,
    is_active: form.value.is_active,
    priority: Number(form.value.priority) || 0,
  };
}

async function onCreate() {
  await store.create(buildPayload());
  showForm.value = false;
  form.value = {
    alias: "",
    provider: "openai",
    model_name: "",
    temperature: "0.7",
    max_tokens: "4096",
    cost_per_1k_input: "0",
    cost_per_1k_output: "0",
    is_active: true,
    priority: "0",
  };
}

onMounted(() => store.fetchList());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">Model Configs</h2>
      <Button variant="outline" @click="showForm = !showForm">
        {{ showForm ? "Cancel" : "+ New Model" }}
      </Button>
    </div>

    <Card v-if="showForm">
      <CardHeader><CardTitle>Create Model Config</CardTitle></CardHeader>
      <CardContent>
        <form class="grid grid-cols-2 gap-3" @submit.prevent="onCreate">
          <div class="space-y-1">
            <label class="text-sm font-medium">Alias</label>
            <Input v-model="form.alias" placeholder="gpt-4o-mini" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Model Name</label>
            <Input v-model="form.model_name" placeholder="gpt-4o-mini-2024-07-18" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Provider</label>
            <select v-model="form.provider" class="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm">
              <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
            </select>
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Priority</label>
            <Input v-model="form.priority" type="number" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Temperature</label>
            <Input v-model="form.temperature" type="number" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Max Tokens</label>
            <Input v-model="form.max_tokens" type="number" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Cost / 1k input ($)</label>
            <Input v-model="form.cost_per_1k_input" type="number" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Cost / 1k output ($)</label>
            <Input v-model="form.cost_per_1k_output" type="number" />
          </div>
          <Button type="submit" class="col-span-2">Create</Button>
        </form>
      </CardContent>
    </Card>

    <div v-if="store.loading" class="text-sm text-muted-foreground">Loading...</div>
    <div v-else-if="store.models.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
      No models configured.
    </div>
    <div v-else class="overflow-hidden rounded-md border">
      <div
        v-for="m in store.models"
        :key="m.id"
        class="cursor-pointer border-b p-4 transition-colors last:border-b-0 hover:bg-muted/50"
        :class="{ 'bg-muted': store.selectedAlias === m.alias }"
        @click="store.select(m.alias)"
      >
        <div class="flex items-center justify-between">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-medium">{{ m.alias }}</span>
              <Badge variant="secondary">{{ m.provider }}</Badge>
              <Badge :variant="m.is_active ? 'default' : 'outline'">
                {{ m.is_active ? "enabled" : "disabled" }}
              </Badge>
            </div>
            <div class="text-sm text-muted-foreground">{{ m.model_name }}</div>
          </div>
          <div class="text-right text-xs text-muted-foreground">
            <div>priority: {{ m.priority }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
