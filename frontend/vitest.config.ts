import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

/**
 * Vitest 单元测试配置（L1 前端单元测试）
 *
 * - environment: jsdom
 * - alias: @ -> src（与 tsconfig/vite 保持一致）
 * - coverage: istanbul；目标 80%（testing.spec.md §9），当前为基线收集阶段
 *   （shared 层已覆盖，domains/views 待补）。门槛暂设基线值，CI 以
 *   continue-on-error 上报，待补齐测试后逐步提升至 80% 并转为阻断门禁。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    // 仅收集 src 下的单元测试；明确排除 e2e（Playwright）与产物目录
    include: ["src/**/*.{test,spec}.ts"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    coverage: {
      provider: "istanbul",
      reporter: ["text", "text-summary", "html"],
      // 基线门槛（当前 shared 层覆盖率 ~90%，domains/views 待补拉低整体）。
      // 防止覆盖率回退；目标 80%，待 domains 测试补齐后上调。
      thresholds: {
        lines: 10,
        functions: 9,
        branches: 10,
        statements: 12,
      },
    },
  },
});
