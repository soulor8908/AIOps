<script setup lang="ts">
import { ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useUserStore } from "@/shared/stores/user";
import { Button, Input, Alert } from "@/shared/ui";

const userStore = useUserStore();
const router = useRouter();
const route = useRoute();

const email = ref("");
const password = ref("");

// P3：用户重新输入时清空上一次登录失败提示，避免错误信息与当前输入不同步
watch([email, password], () => {
  if (userStore.error) userStore.error = null;
});

// P3：redirect 白名单校验，防止开放重定向（?redirect=//evil.com）
// 仅允许以单个 / 开头的站内相对路径，拒绝 //（协议相对 URL）和 /\（绕过）
function safeRedirect(redirect: unknown): string {
  if (typeof redirect !== "string" || !redirect) return "/";
  if (redirect === "/") return "/";
  if (/^\/[^/\\]/.test(redirect)) return redirect;
  return "/";
}

async function onSubmit() {
  try {
    await userStore.login(email.value, password.value);
    router.replace(safeRedirect(route.query.redirect));
  } catch {
    // error 已写入 userStore.error，Alert 展示
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-background p-4">
    <div class="w-full max-w-sm space-y-6">
      <div class="space-y-1 text-center">
        <div class="mx-auto flex h-10 w-10 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          AI
        </div>
        <h1 class="text-xl font-semibold">AIOps Console</h1>
        <p class="text-sm text-muted-foreground">Sign in to your account</p>
      </div>

      <Alert v-if="userStore.error" :message="userStore.error" />

      <form class="space-y-4" @submit.prevent="onSubmit">
        <div class="space-y-1">
          <label class="text-sm font-medium" for="email">Email</label>
          <Input
            id="email"
            v-model="email"
            type="email"
            placeholder="you@example.com"
            autocomplete="username"
            required
          />
        </div>
        <div class="space-y-1">
          <label class="text-sm font-medium" for="password">Password</label>
          <Input
            id="password"
            v-model="password"
            type="password"
            placeholder="••••••••"
            autocomplete="current-password"
            required
          />
        </div>
        <Button type="submit" class="w-full" :disabled="userStore.loading">
          {{ userStore.loading ? "Signing in..." : "Sign in" }}
        </Button>
      </form>
    </div>
  </div>
</template>
