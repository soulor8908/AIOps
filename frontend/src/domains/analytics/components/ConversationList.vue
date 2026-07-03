<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAnalyticsStore } from "../store";
import { Button, Input, Badge, Alert, Skeleton } from "@/shared/ui";
import { formatDate, formatNumber, formatCost } from "@/shared/utils";

const store = useAnalyticsStore();
const startDate = ref("");
const endDate = ref("");

function onFilter() {
  // P3：修复原 onFilter 传空对象 {} 忽略日期的 bug，真实透传 start_date / end_date
  void store.fetchConversations({
    start_date: startDate.value || undefined,
    end_date: endDate.value || undefined,
  });
}

onMounted(() => store.fetchConversations());
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <Input v-model="startDate" type="date" class="w-44" />
      <Input v-model="endDate" type="date" class="w-44" />
      <Button @click="onFilter">Filter</Button>
      <Button variant="outline" @click="store.fetchConversations()">Refresh</Button>
      <span class="text-sm text-muted-foreground">{{ formatNumber(store.total) }} total</span>
    </div>

    <Alert v-if="store.error" :message="store.error" @retry="store.fetchConversations()" />

    <div v-if="store.loading" class="space-y-2">
      <Skeleton v-for="i in 5" :key="i" class="h-10 w-full" />
    </div>

    <div v-else-if="!store.error && store.conversations.length === 0" class="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">
      No conversations found.
    </div>
    <div v-else class="overflow-x-auto rounded-md border">
      <table class="w-full text-sm">
        <thead class="bg-muted/50 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th class="p-3">ID</th>
            <th class="p-3">Model</th>
            <th class="p-3">Messages</th>
            <th class="p-3">Tokens</th>
            <th class="p-3">Cost</th>
            <th class="p-3">Created</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="c in store.conversations"
            :key="c.id"
            class="border-t hover:bg-muted/30"
          >
            <td class="max-w-[12rem] truncate p-3 font-mono text-xs">{{ c.id }}</td>
            <td class="p-3"><Badge variant="secondary">{{ c.model_alias }}</Badge></td>
            <td class="p-3">{{ formatNumber(c.messages.length) }}</td>
            <td class="p-3">{{ formatNumber(c.total_tokens) }}</td>
            <td class="p-3">{{ formatCost(c.total_cost) }}</td>
            <td class="p-3 text-xs text-muted-foreground">{{ formatDate(c.created_at) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
