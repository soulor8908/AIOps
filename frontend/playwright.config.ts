import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E 配置（L3 测试）
 *
 * - testDir: ./e2e
 * - baseURL: http://localhost:5173 (Vite dev server)
 * - 超时 30s，断言超时 10s
 * - retries: 本地 1 次，CI 2 次
 * - 仅 chromium
 * - webServer 自动拉起 `npm run dev`
 *
 * @see https://playwright.dev/docs/test-configuration
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  // CI 中禁止 .only，避免误跳过用例
  forbidOnly: !!process.env.CI,
  // 本地失败重试 1 次，CI 中重试 2 次
  retries: process.env.CI ? 2 : 1,
  // CI 中串行执行，本地按机器并发
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",

  // 全局超时
  timeout: 30_000,

  expect: {
    // 断言超时
    timeout: 10_000,
  },

  use: {
    baseURL: "http://localhost:5173",
    // 首次重试时收集 trace，便于排障
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    // CI 中不显示浏览器窗口（headless）；本地默认同样 headless
    headless: true,
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // 自动启动 Vite dev server（端口 5173），就绪后再跑用例
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    // CI 中不复用既有 server，本地可复用
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
