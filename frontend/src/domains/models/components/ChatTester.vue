<script setup lang="ts">
import { ref } from "vue";
import { useModelStore } from "../store";
import { Alert, Button, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { chatCompletion } from "../api";
import type { ChatMessage } from "@/shared/api/types";

const store = useModelStore();
const userInput = ref("");
const response = ref("");
const usage = ref<string>("");
const cost = ref<string>("");
const sending = ref(false);
const error = ref<string | null>(null);

async function onSend() {
  if (!store.selected || !userInput.value.trim()) return;
  sending.value = true;
  response.value = "";
  error.value = null;
  try {
    const messages: ChatMessage[] = [{ role: "user", content: userInput.value }];
    const res = await chatCompletion(store.selected.alias, { messages });
    response.value = res.content;
    usage.value = JSON.stringify(res.usage);
    cost.value = res.cost;
  } catch (e) {
    error.value = e instanceof Error ? e.message : "Request failed";
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <div v-if="!store.selected" class="flex h-full items-center justify-center text-sm text-muted-foreground">
    Select a model to test chat completion.
  </div>

  <div v-else class="space-y-4">
    <Card>
      <CardHeader>
        <div class="flex items-center justify-between">
          <CardTitle>Chat Tester</CardTitle>
          <Badge variant="secondary">{{ store.selected.alias }}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div class="mb-3 text-sm text-muted-foreground">
          {{ store.selected.provider }} / {{ store.selected.model_name }}
        </div>
        <div class="flex items-center gap-2">
          <input
            v-model="userInput"
            placeholder="Type a message..."
            class="h-9 flex-1 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            @keyup.enter="onSend"
          />
          <Button :disabled="sending" @click="onSend">
            {{ sending ? "Sending..." : "Send" }}
          </Button>
        </div>

        <Alert v-if="error" :message="error" />

        <div v-if="response || cost !== ''" class="mt-4 space-y-2">
          <div class="text-xs font-medium text-muted-foreground">Response</div>
          <pre class="max-h-60 overflow-auto rounded-md bg-muted p-3 text-sm whitespace-pre-wrap">{{ response || "(empty)" }}</pre>
          <div v-if="cost !== ''" class="flex gap-4 text-xs text-muted-foreground">
            <span>cost: {{ cost }}</span>
            <span>usage: {{ usage }}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
