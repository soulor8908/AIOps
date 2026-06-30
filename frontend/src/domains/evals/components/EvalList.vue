<script setup lang="ts">
import { onMounted } from "vue";
import { useEvalStore } from "../store";
import { Button, Badge } from "@/shared/ui";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatDate, formatPercent } from "@/shared/utils";
import type { EvalRunOut } from "@/shared/api/types";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

const store = useEvalStore();

function statusVariant(status: EvalRunOut["status"]): BadgeVariant {
  if (status === "done") return "default";
  if (status === "failed") return "destructive";
  if (status === "running") return "secondary";
  return "outline";
}

async function onRun(id: string) {
  await store.execute(id);
}

onMounted(() => store.fetchList());
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-lg font-semibold">Eval Runs</h2>
      <Button variant="outline" @click="store.fetchList()">Refresh</Button>
    </div>

    <div v-if="store.loading" class="text-sm text-muted-foreground">Loading...</div>
    <div v-else-if="store.runs.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
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
              <div class="text-xs text-muted-foreground">Prompt Version</div>
              <div>{{ run.prompt_version_id }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Model</div>
              <div>{{ run.model_alias }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Pass Rate</div>
              <div>{{ formatPercent(run.pass_rate) }}</div>
            </div>
            <div>
              <div class="text-xs text-muted-foreground">Avg Score</div>
              <div>{{ run.avg_score.toFixed(2) }}</div>
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
