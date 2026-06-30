<script setup lang="ts">
import { ref } from "vue";
import { useEvalStore } from "../store";
import { Button, Input } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import type { EvalRunCreate } from "@/shared/api/types";

const store = useEvalStore();
const emit = defineEmits<{ created: [] }>();

const form = ref({
  prompt_version_id: "",
  model_alias: "",
  cases: "",
});

const submitting = ref(false);
const error = ref<string | null>(null);

async function onSubmit() {
  const pvId = Number(form.value.prompt_version_id);
  if (!form.value.model_alias.trim() || !pvId) {
    error.value = "Prompt version ID and model alias are required.";
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    const payload: EvalRunCreate = {
      prompt_version_id: pvId,
      model_alias: form.value.model_alias,
      cases: form.value.cases
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    await store.create(payload);
    emit("created");
    form.value = { prompt_version_id: "", model_alias: "", cases: "" };
  } catch (e) {
    error.value = e instanceof Error ? e.message : "Failed to create eval";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <Card>
    <CardHeader><CardTitle>Create Eval Run</CardTitle></CardHeader>
    <CardContent>
      <form class="space-y-3" @submit.prevent="onSubmit">
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1">
            <label class="text-sm font-medium">Prompt Version ID</label>
            <Input v-model="form.prompt_version_id" type="number" placeholder="1" />
          </div>
          <div class="space-y-1">
            <label class="text-sm font-medium">Model Alias</label>
            <Input v-model="form.model_alias" placeholder="gpt-4o-mini" />
          </div>
        </div>
        <div class="space-y-1">
          <label class="text-sm font-medium">Cases (one per line)</label>
          <textarea
            v-model="form.cases"
            rows="5"
            placeholder="case-1&#10;case-2&#10;case-3"
            class="w-full rounded-md border border-input bg-transparent p-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <p v-if="error" class="text-sm text-destructive">{{ error }}</p>
        <div class="flex justify-end">
          <Button type="submit" :disabled="submitting">
            {{ submitting ? "Creating..." : "Create Eval" }}
          </Button>
        </div>
      </form>
    </CardContent>
  </Card>
</template>
