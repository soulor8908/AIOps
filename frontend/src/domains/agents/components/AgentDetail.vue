<script setup lang="ts">
import { ref, watch } from "vue";
import { useAgentStore } from "../store";
import { Button, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate, formatNumber } from "@/shared/utils";

const store = useAgentStore();
const inputText = ref("");
const result = ref<string>("");

// P2：切换 agent 时清空本地执行结果与上次执行状态，避免显示上一个 agent 的输出
watch(
  () => store.selectedId,
  (id) => {
    if (id !== null) {
      result.value = "";
      inputText.value = "";
      store.lastResult = null;
    }
  },
);

async function onExecute() {
  if (!store.selected) return;
  try {
    const res = await store.execute(store.selected.id, inputText.value);
    result.value =
      typeof res.final_answer === "string"
        ? res.final_answer
        : JSON.stringify(res.final_answer, null, 2);
  } catch {
    result.value = "Execution failed. See error.";
  }
}
</script>

<template>
  <div v-if="!store.selected" class="flex h-full items-center justify-center text-sm text-muted-foreground">
    Select an agent to view details and execute.
  </div>

  <div v-else class="space-y-4">
    <Card>
      <CardHeader>
        <div class="flex items-center justify-between">
          <CardTitle>{{ store.selected.name }}</CardTitle>
          <Badge variant="secondary">{{ store.selected.model_alias }}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <p class="text-sm text-muted-foreground">
          {{ store.selected.description || "No description" }}
        </p>
        <div class="mt-3">
          <div class="mb-1 text-xs font-medium text-muted-foreground">System Prompt</div>
          <pre class="max-h-60 overflow-auto rounded-md bg-muted p-3 text-sm whitespace-pre-wrap">{{ store.selected.system_prompt }}</pre>
        </div>
        <div v-if="store.selected.tools.length" class="mt-3">
          <div class="mb-1 text-xs font-medium text-muted-foreground">Tools</div>
          <div class="flex flex-wrap gap-1">
            <Badge v-for="(t, idx) in store.selected.tools" :key="String(t.name ?? idx)" variant="outline">{{ t.name ?? t }}</Badge>
          </div>
        </div>
        <div class="mt-3 text-xs text-muted-foreground">
          Created {{ formatDate(store.selected.created_at) }}
        </div>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>Execute</CardTitle></CardHeader>
      <CardContent>
        <div class="space-y-3">
          <textarea
            v-model="inputText"
            rows="3"
            placeholder="Enter input message..."
            class="w-full rounded-md border border-input bg-transparent p-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <Button :disabled="!inputText.trim() || store.executing" @click="onExecute">
            {{ store.executing ? "Running..." : "Run Agent" }}
          </Button>
          <div v-if="store.lastResult" class="space-y-2">
            <div class="flex gap-4 text-xs text-muted-foreground">
              <span>Status: {{ store.lastResult.success ? "success" : "failed" }}</span>
              <span>Tokens: {{ formatNumber(store.lastResult.total_tokens) }}</span>
            </div>
            <pre class="max-h-60 overflow-auto rounded-md bg-muted p-3 text-sm whitespace-pre-wrap">{{ result }}</pre>
          </div>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
