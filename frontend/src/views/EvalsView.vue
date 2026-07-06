<script setup lang="ts">
import { ref } from "vue";
import EvalList from "@/domains/evals/components/EvalList.vue";
import EvalRunner from "@/domains/evals/components/EvalRunner.vue";
import EvalSamples from "@/domains/evals/components/EvalSamples.vue";
import OnlineEvalRunner from "@/domains/evals/components/OnlineEvalRunner.vue";
import { Button } from "@/shared/ui";

const showRunner = ref(false);
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">Eval Suite</h1>
        <p class="text-sm text-muted-foreground">
          Offline eval runs + online eval 闭环: 生产采样 → LLM judge → 回归检测.
        </p>
      </div>
      <Button variant="outline" @click="showRunner = !showRunner">
        {{ showRunner ? "Close" : "+ New Eval" }}
      </Button>
    </div>

    <EvalRunner v-if="showRunner" @created="showRunner = false" />
    <EvalList />

    <!-- E2: Online eval 闭环 -->
    <div class="space-y-4">
      <h2 class="text-lg font-semibold">Online Eval Loop</h2>
      <div class="grid gap-4 lg:grid-cols-2">
        <EvalSamples />
        <OnlineEvalRunner />
      </div>
    </div>
  </div>
</template>
