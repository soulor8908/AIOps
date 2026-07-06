<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useEvalStore } from "../store";
import { useUserStore } from "@/shared/stores/user";
import { Button, Badge, Alert, Skeleton, Input } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate } from "@/shared/utils";
import * as evalsApi from "../api";

const store = useEvalStore();
const userStore = useUserStore();

const isAdmin = computed(() => userStore.user?.role === "admin");
const filterJudged = ref<"" | "true" | "false">("false");

// 录样表单（admin-only）
const showSampleForm = ref(false);
const sampleForm = ref({
  input: "",
  actual_output: "",
  expected_output: "",
  trigger_source: "http" as const,
});
const sampleSubmitting = ref(false);
const sampleError = ref<string | null>(null);

async function reload() {
  const query =
    filterJudged.value === ""
      ? {}
      : { judged: filterJudged.value === "true" };
  // 切换过滤时清空选择（避免跨 filter 误选）
  store.clearSampleSelection();
  await store.fetchSamples(query);
}

async function submitSample() {
  if (!sampleForm.value.input.trim() || !sampleForm.value.actual_output.trim()) {
    sampleError.value = "Input and actual_output are required.";
    return;
  }
  sampleSubmitting.value = true;
  sampleError.value = null;
  try {
    await evalsApi.createSample({
      input: sampleForm.value.input,
      actual_output: sampleForm.value.actual_output,
      expected_output: sampleForm.value.expected_output || undefined,
      trigger_source: sampleForm.value.trigger_source,
    });
    await store.fetchSamples({ judged: false });
    showSampleForm.value = false;
    sampleForm.value = {
      input: "",
      actual_output: "",
      expected_output: "",
      trigger_source: "http",
    };
  } catch (e) {
    sampleError.value = e instanceof Error ? e.message : "Failed to record sample";
  } finally {
    sampleSubmitting.value = false;
  }
}

function priorityVariant(p: number): "default" | "secondary" | "destructive" {
  if (p >= 2) return "destructive";
  if (p === 1) return "secondary";
  return "default";
}

onMounted(() => reload());
</script>

<template>
  <Card>
    <CardHeader>
      <div class="flex items-center justify-between">
        <CardTitle class="text-base">
          Production Samples
          <span class="ml-2 text-xs font-normal text-muted-foreground">
            ({{ store.samples.length }})
          </span>
        </CardTitle>
        <div class="flex items-center gap-2">
          <select
            v-model="filterJudged"
            class="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
            @change="reload"
          >
            <option value="">All</option>
            <option value="false">Pending</option>
            <option value="true">Judged</option>
          </select>
          <Button variant="outline" size="sm" @click="reload">Refresh</Button>
          <Button v-if="isAdmin" variant="outline" size="sm" @click="showSampleForm = !showSampleForm">
            {{ showSampleForm ? "Close" : "+ Record" }}
          </Button>
        </div>
      </div>
    </CardHeader>
    <CardContent>
      <!-- admin 录样表单 -->
      <form v-if="showSampleForm && isAdmin" class="mb-4 space-y-2 rounded-md border p-3" @submit.prevent="submitSample">
        <div class="space-y-1">
          <label class="text-xs font-medium">Input</label>
          <textarea
            v-model="sampleForm.input"
            rows="2"
            class="w-full rounded-md border border-input bg-transparent p-2 text-sm"
            placeholder="用户输入"
          />
        </div>
        <div class="space-y-1">
          <label class="text-xs font-medium">Actual Output</label>
          <textarea
            v-model="sampleForm.actual_output"
            rows="2"
            class="w-full rounded-md border border-input bg-transparent p-2 text-sm"
            placeholder="Agent 实际输出"
          />
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div class="space-y-1">
            <label class="text-xs font-medium">Expected (optional)</label>
            <Input v-model="sampleForm.expected_output" />
          </div>
          <div class="space-y-1">
            <label class="text-xs font-medium">Trigger Source</label>
            <select
              v-model="sampleForm.trigger_source"
              class="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="http">http</option>
              <option value="scheduled">scheduled</option>
              <option value="a2a">a2a</option>
              <option value="workflow">workflow</option>
            </select>
          </div>
        </div>
        <p v-if="sampleError" class="text-xs text-destructive">{{ sampleError }}</p>
        <div class="flex justify-end">
          <Button type="submit" size="sm" :disabled="sampleSubmitting">
            {{ sampleSubmitting ? "Recording..." : "Record Sample" }}
          </Button>
        </div>
      </form>

      <Alert v-if="store.error" :message="store.error" />

      <div v-if="store.samplesLoading" class="space-y-2">
        <Skeleton v-for="i in 3" :key="i" class="h-16 w-full" />
      </div>

      <div v-else-if="store.samples.length === 0" class="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
        No samples. Production agent executions will be sampled here automatically.
      </div>

      <div v-else class="space-y-2">
        <!-- 选择工具栏（仅 admin + pending 视图可见） -->
        <div v-if="isAdmin && filterJudged === 'false'" class="flex items-center gap-2 text-xs text-muted-foreground">
          <Button variant="ghost" size="sm" @click="store.selectAllPendingSamples">Select all pending</Button>
          <Button
            variant="ghost"
            size="sm"
            @click="store.clearSampleSelection"
            :disabled="store.selectedSampleIds.size === 0"
          >
            Clear ({{ store.selectedSampleIds.size }})
          </Button>
        </div>

        <div
          v-for="sample in store.samples"
          :key="sample.id"
          class="rounded-md border p-3 text-sm"
          :class="store.selectedSampleIds.has(sample.id) ? 'border-primary bg-primary/5' : ''"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="flex items-center gap-2">
              <input
                v-if="isAdmin && !sample.judged"
                type="checkbox"
                :checked="store.selectedSampleIds.has(sample.id)"
                class="mt-0.5"
                @change="store.toggleSampleSelection(sample.id)"
              />
              <Badge :variant="priorityVariant(sample.priority)" class="text-xs">
                P{{ sample.priority }}
              </Badge>
              <Badge v-if="sample.judged" variant="secondary" class="text-xs">judged</Badge>
              <Badge variant="outline" class="text-xs">{{ sample.trigger_source }}</Badge>
            </div>
            <span class="text-xs text-muted-foreground">{{ formatDate(sample.sampled_at) }}</span>
          </div>
          <div class="mt-2 line-clamp-2 text-xs text-muted-foreground">
            <span class="font-medium">Input:</span> {{ sample.input }}
          </div>
          <div class="mt-1 line-clamp-2 text-xs">
            <span class="font-medium text-muted-foreground">Output:</span> {{ sample.actual_output }}
          </div>
          <div v-if="sample.judged" class="mt-2 flex items-center gap-2 text-xs">
            <span class="text-muted-foreground">Score:</span>
            <span :class="sample.judge_score != null && sample.judge_score < 0.7 ? 'text-destructive font-medium' : ''">
              {{ sample.judge_score != null ? sample.judge_score.toFixed(2) : "-" }}
            </span>
            <span v-if="sample.judge_reason" class="text-muted-foreground">— {{ sample.judge_reason }}</span>
          </div>
        </div>
      </div>
    </CardContent>
  </Card>
</template>
