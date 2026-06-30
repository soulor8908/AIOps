import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

/**
 * Vitest 单元测试配置（L1 前端单元测试）
 *
 * - environment: jsdom
 * - alias: @ -> src（与 tsconfig/vite 保持一致）
 * - coverage: istanbul，门槛 80%
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
      reporter: ["text", "html"],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80,
      },
    },
  },
});
