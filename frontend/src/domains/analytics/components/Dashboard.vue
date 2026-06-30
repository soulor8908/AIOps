<script setup lang="ts">
import { onMounted, computed } from "vue";
import { useAnalyticsStore } from "../store";
import { Card, CardHeader, CardTitle, CardContent } from "@/shared/ui";
import { formatNumber, formatCost, formatPercent } from "@/shared/utils";

const store = useAnalyticsStore();

onMounted(() => store.fetchMetrics());

interface MetricCard {
  label: string;
  value: string;
  hint?: string;
}

const cards = computed<MetricCard[]>(() => {
  const m = store.metrics;
  return [
    {
      label: "Total Conversations",
      value: m ? formatNumber(m.total_conversations) : "-",
    },
    {
      label: "Total Tokens",
      value: m ? formatNumber(m.total_tokens) : "-",
    },
    {
      label: "Total Cost",
      value: m ? formatCost(m.total_cost_usd) : "-",
    },
    {
      label: "Avg Latency",
      value: m && m.avg_latency_ms != null ? `${formatNumber(m.avg_latency_ms)} ms` : "-",
    },
    {
      label: "Avg Quality",
      value:
        m && m.avg_quality_score != null
          ? formatPercent(m.avg_quality_score)
          : "-",
    },
  ];
});

const modelDist = computed(() => {
  const dist = store.metrics?.model_distribution ?? {};
  return Object.entries(dist).map(([model, count]) => ({ model, count }));
});

const daily = computed(() => store.metrics?.daily_stats ?? []);
</script>

<template>
  <div class="space-y-6">
    <div v-if="store.loading && !store.metrics" class="text-sm text-muted-foreground">
      Loading metrics...
    </div>

    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      <Card v-for="card in cards" :key="card.label">
        <CardHeader class="pb-2">
          <CardTitle class="text-sm font-medium text-muted-foreground">
            {{ card.label }}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div class="text-2xl font-bold">{{ card.value }}</div>
        </CardContent>
      </Card>
    </div>

    <div class="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Model Distribution</CardTitle></CardHeader>
        <CardContent>
          <div v-if="modelDist.length === 0" class="text-sm text-muted-foreground">
            No data.
          </div>
          <div v-else class="space-y-2">
            <div v-for="d in modelDist" :key="d.model" class="flex items-center justify-between text-sm">
              <span>{{ d.model }}</span>
              <span class="font-medium">{{ formatNumber(d.count) }}</span>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Daily Stats</CardTitle></CardHeader>
        <CardContent>
          <div v-if="daily.length === 0" class="text-sm text-muted-foreground">
            No data.
          </div>
          <div v-else class="max-h-64 space-y-2 overflow-y-auto">
            <div
              v-for="d in daily"
              :key="d.date"
              class="flex items-center justify-between border-b py-1 text-sm last:border-b-0"
            >
              <span>{{ d.date }}</span>
              <span class="text-muted-foreground">
                {{ formatNumber(d.conversations) }} conv / {{ formatCost(d.cost_usd) }}
              </span>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  </div>
</template>
