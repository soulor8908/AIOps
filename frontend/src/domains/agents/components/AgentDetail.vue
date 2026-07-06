<script setup lang="ts">
import { ref, watch } from "vue";
import { useAgentStore } from "../store";
import { Button, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate, formatNumber } from "@/shared/utils";

const store = useAgentStore();
const inputText = ref("");
const result = ref<string>("");
// 流式开关：默认 true（后端 P2-8 流式端点已就绪），可切回同步执行
const useStream = ref(true);

// P2：切换 agent 时清空本地执行结果与上次执行状态，避免显示上一个 agent 的输出
watch(
  () => store.selectedId,
  (id) => {
    if (id !== null) {
      result.value = "";
      inputText.value = "";
      store.lastResult = null;
      store.streamText = "";
      store.streamTraces = [];
    }
  },
);

async function onExecute() {
  if (!store.selected) return;
  result.value = "";
  try {
    if (useStream.value) {
      const res = await store.executeStream(store.selected.id, inputText.value);
      result.value =
        typeof res.final_answer === "string"
          ? res.final_answer
          : JSON.stringify(res.final_answer, null, 2);
    } else {
      const res = await store.execute(store.selected.id, inputText.value);
      result.value =
        typeof res.final_answer === "string"
          ? res.final_answer
          : JSON.stringify(res.final_answer, null, 2);
    }
  } catch {
    result.value = "Execution failed. See error.";
  }
}

function onCancelStream() {
  store.cancelStream();
  result.value = "[cancelled]";
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
          <div class="flex items-center justify-between">
            <label class="flex items-center gap-2 text-xs text-muted-foreground">
              <input type="checkbox" v-model="useStream" />
              Stream mode (逐 token 渲染)
            </label>
            <div class="flex gap-2">
              <Button
                v-if="store.streaming"
                variant="outline"
                size="sm"
                @click="onCancelStream"
              >
                Cancel
              </Button>
              <Button
                :disabled="!inputText.trim() || store.executing || store.streaming"
                @click="onExecute"
              >
                {{ store.executing || store.streaming ? "Running..." : "Run Agent" }}
              </Button>
            </div>
          </div>

          <!-- 流式过程：工具调用 + 观察 -->
          <div v-if="store.streamTraces.length" class="space-y-1">
            <div class="text-xs font-medium text-muted-foreground">Execution Traces</div>
            <div
              v-for="(t, idx) in store.streamTraces"
              :key="idx"
              class="rounded-md border p-2 text-xs"
            >
              <Badge :variant="t.type === 'tool' ? 'default' : 'secondary'" class="mr-2 text-xs">
                {{ t.type }}
              </Badge>
              <span class="font-mono">{{ t.content }}</span>
            </div>
          </div>

          <!-- 流式输出（逐 token 拼接） -->
          <div v-if="store.streaming && store.streamText" class="space-y-2">
            <div class="text-xs font-medium text-muted-foreground">Streaming...</div>
            <pre class="max-h-60 overflow-auto rounded-md bg-muted p-3 text-sm whitespace-pre-wrap">{{ store.streamText }}<span class="animate-pulse">▌</span></pre>
          </div>

          <!-- 最终结果 -->
          <div v-if="store.lastResult && !store.streaming" class="space-y-2">
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
