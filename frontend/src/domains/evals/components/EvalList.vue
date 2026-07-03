<script setup lang="ts">
import { onMounted } from "vue";
import { useEvalStore } from "../store";
import { Button, Badge, Alert, Skeleton } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate, formatPercent } from "@/shared/utils";
import type { EvalRunOut } from "@/shared/api/types";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const store = useEvalStore();

function statusVariant(status: EvalRunOut["status"]): BadgeVariant {
  if (status === "passed") return "default";
  if (status === "failed" || status === "error") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

async function onRun(id: string) {
  try {
    await store.execute(id);
  } catch {
    // error 已写入 store.error，Alert 会展示
  }
}

onMounted(() => store.fetchList());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">Eval Runs</h2>
      <Button variant="outline" @click="store.fetchList()">Refresh</Button>
    </div>

    <Alert v-if="store.error" :message="store.error" @retry="store.fetchList()" />

    <div v-if="store.loading" class="space-y-3">
      <Skeleton v-for="i in 3" :key="i" class="h-32 w-full" />
    </div>

    <div v-else-if="!store.error && store.runs.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
      No eval runs yet. Use the runner to create one.
    </div>
    <div v-else class="space-y-3">
      <Card v-for="run in store.runs" :key="run.id">
        <CardHeader>
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <CardTitle class="font-mono text-sm">{{ run.id }}</CardTitle>
              <Badge :variant="statusVariant(run.status)">{{ run.status }}</Badge>
            </div>
            <Button
              v-if="run.status === 'pending'"
              size="sm"
              :disabled="store.running"
              @click="onRun(run.id)"
            >
              {{ store.running ? "Running..." : "Run" }}
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div class="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            <div>
              <div class="text-xs text-muted-foreground">Judge</div>
              <div>{{ run.judge_type }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Passed</div>
              <div>{{ run.pass_count }} / {{ run.pass_count + run.fail_count }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Pass Rate</div>
              <div>{{ formatPercent(run.pass_count + run.fail_count > 0 ? run.pass_count / (run.pass_count + run.fail_count) : null) }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Score</div>
              <div>{{ run.score != null ? run.score.toFixed(2) : "-" }}</div>
            </div>
          </div>
          <div class="mt-2 text-xs text-muted-foreground">
            Created {{ formatDate(run.created_at) }}
          </div>
        </CardContent>
      </Card>
    </div>
  </div>
</template>
