<script setup lang="ts">
import { ref } from "vue";
import { usePromptStore } from "../store";
import { Button, Input } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import type { PromptCreate } from "@/shared/api/types";

const store = usePromptStore();
const emit = defineEmits<{ created: [] }>();

const form = ref<PromptCreate>({
  name: "",
  description: "",
  content: "",
  variables: [],
});

const variablesInput = ref("");
const submitting = ref(false);
const error = ref<string | null>(null);

async function onSubmit() {
  if (!form.value.name.trim() || !form.value.content.trim()) {
    error.value = "Name and content are required.";
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    form.value.variables = variablesInput.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    await store.create(form.value);
    emit("created");
    form.value = { name: "", description: "", content: "", variables: [] };
    variablesInput.value = "";
  } catch (e) {
    error.value = e instanceof Error ? e.message : "Failed to create prompt";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <Card>
    <CardHeader>
      <CardTitle>New Prompt</CardTitle>
    </CardHeader>
    <CardContent>
      <form class="space-y-3" @submit.prevent="onSubmit">
        <div class="space-y-1">
          <label class="text-sm font-medium">Name</label>
          <Input v-model="form.name" placeholder="prompt-name" />
        </div>
        <div class="space-y-1">
          <label class="text-sm font-medium">Description</label>
          <Input v-model="form.description" placeholder="optional description" />
        </div>
        <div class="space-y-1">
          <label class="text-sm font-medium">Variables (comma-separated)</label>
          <Input v-model="variablesInput" placeholder="topic, tone, language" />
        </div>
        <div class="space-y-1">
          <label class="text-sm font-medium">Content</label>
          <textarea
            v-model="form.content"
            rows="6"
            placeholder="You are a helpful assistant..."
            class="w-full rounded-md border border-input bg-transparent p-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
        <div class="flex justify-end gap-2">
          <Button type="submit" :disabled="submitting">
            {{ submitting ? "Creating..." : "Create Prompt" }}
          </Button>
        </div>
      </form>
    </CardContent>
  </Card>
</template>
