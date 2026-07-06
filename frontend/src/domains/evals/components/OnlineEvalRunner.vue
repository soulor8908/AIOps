<script setup lang="ts">
import { computed, ref } from "vue";
import { useEvalStore } from "../store";
import { useUserStore } from "@/shared/stores/user";
import { Button, Input, Alert, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import type { OnlineEvalRequest, EvalRunOut } from "@/shared/api/types";

const store = useEvalStore();
const userStore = useUserStore();

const isAdmin = computed(() => userStore.user?.role === "admin");

const form = ref({
  golden_run_name: "",
  judge_type: "llm" as OnlineEvalRequest["judge_type"],
  run_name: "",
  // true = 仅评估选中的样本；false = 评估所有未 judged 样本
  use_selected_only: true,
});

const error = ref<string | null>(null);
const lastRun = ref<EvalRunOut | null>(null);

const selectedCount = computed(() => store.selectedSampleIds.size);

async function onSubmit() {
  if (!form.value.golden_run_name.trim()) {
    error.value = "Golden run name is required (用于匹配离线基线).";
    return;
  }
  error.value = null;
  lastRun.value = null;

  const payload: OnlineEvalRequest = {
    golden_run_name: form.value.golden_run_name.trim(),
    judge_type: form.value.judge_type,
    run_name: form.value.run_name.trim() || undefined,
  };
  if (form.value.use_selected_only) {
    if (selectedCount.value === 0) {
      error.value = "未选中任何样本。请先在 Samples 卡片中勾选,或选择 'All pending'.";
      return;
    }
    payload.sample_ids = Array.from(store.selectedSampleIds);
  }

  try {
    lastRun.value = await store.runOnlineEval(payload);
  } catch (e) {
    error.value = e instanceof Error ? e.message : "Online eval failed";
  }
}
</script>

<template>
  <Card v-if="isAdmin">
    <CardHeader>
      <CardTitle class="text-base">Online Eval</CardTitle>
    </CardHeader>
    <CardContent>
      <p class="mb-3 text-xs text-muted-foreground">
        对生产采样样本执行 LLM judge,与离线 golden run 对比检测回归。
        评估结果写入 EvalRun 列表(含 baseline / regression 标记)。
      </p>

      <form class="space-y-3" @submit.prevent="onSubmit">
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1">
            <label class="text-xs font-medium">Golden Run Name *</label>
            <Input v-model="form.golden_run_name" placeholder="golden-baseline-v1" />
          </div>
          <div class="space-y-1">
            <label class="text-xs font-medium">Run Name (optional)</label>
            <Input v-model="form.run_name" placeholder="默认同 golden_run_name" />
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1">
            <label class="text-xs font-medium">Judge Type</label>
            <select
              v-model="form.judge_type"
              class="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option value="llm">llm</option>
              <option value="exact">exact</option>
              <option value="contains">contains</option>
              <option value="semantic">semantic</option>
            </select>
          </div>
          <div class="space-y-1">
            <label class="text-xs font-medium">Sample Scope</label>
            <select
              v-model="form.use_selected_only"
              class="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            >
              <option :value="true">Selected only ({{ selectedCount }})</option>
              <option :value="false">All pending</option>
            </select>
          </div>
        </div>

        <Alert v-if="error" :message="error" />

        <div class="flex items-center justify-between">
          <div v-if="lastRun" class="flex items-center gap-2 text-xs">
            <Badge v-if="lastRun.is_regression" variant="destructive">regression</Badge>
            <Badge v-else variant="default">passed</Badge>
            <span class="text-muted-foreground">
              Score: {{ lastRun.score != null ? lastRun.score.toFixed(2) : "-" }}
              <span v-if="lastRun.baseline_score != null">
                / Baseline: {{ lastRun.baseline_score.toFixed(2) }}
              </span>
            </span>
          </div>
          <Button type="submit" size="sm" :disabled="store.onlineEvalRunning" class="ml-auto">
            {{ store.onlineEvalRunning ? "Running..." : "Run Online Eval" }}
          </Button>
        </div>
      </form>
    </CardContent>
  </Card>
</template>
