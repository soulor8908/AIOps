<script setup lang="ts">
import { ref } from "vue";
import EvalList from "@/domains/evals/components/EvalList.vue";
import EvalRunner from "@/domains/evals/components/EvalRunner.vue";
import { Button } from "@/shared/ui";

const showRunner = ref(false);
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-bold tracking-tight">Eval Suite</h1>
        <p class="text-sm text-muted-foreground">
          Create and execute automated eval runs with rule and judge checks.
        </p>
      </div>
      <Button variant="outline" @click="showRunner = !showRunner">
        {{ showRunner ? "Close" : "+ New Eval" }}
      </Button>
    </div>

    <EvalRunner v-if="showRunner" @created="showRunner = false" />
    <EvalList />
  </div>
</template>
