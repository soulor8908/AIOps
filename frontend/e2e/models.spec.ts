import { test, expect, goTo, login, mockApiGet, mockApiMethod } from "./fixtures";

/** Model Router 列表 mock 数据（与 ModelConfigOut 对齐）。 */
const MODELS = [
  {
    id: 1,
    alias: "gpt-4o",
    provider_name: "openai",
    model_id: "gpt-4o-2024-07-18",
    temperature: 0.7,
    max_tokens: 4096,
    cost_per_1k_input: 0.005,
    cost_per_1k_output: 0.015,
    routing_strategy: "direct",
    fallback_models: [] as string[],
    quota_daily: 1000000,
    enabled: true,
    created_at: "2026-06-29T10:00:00Z",
  },
  {
    id: 2,
    alias: "claude-sonnet",
    provider_name: "anthropic",
    model_id: "claude-3-5-sonnet",
    temperature: 0.7,
    max_tokens: 4096,
    cost_per_1k_input: 0.003,
    cost_per_1k_output: 0.015,
    routing_strategy: "least_cost",
    fallback_models: [] as string[],
    quota_daily: 500000,
    enabled: false,
    created_at: "2026-06-28T10:00:00Z",
  },
];

test.beforeEach(async ({ page }) => {
  await login(page);
  await mockApiGet(page, "/api/v1/models", { items: MODELS, total: MODELS.length });
});

test.describe("Model Router — 关键路径", () => {
  test("view model list", async ({ page }) => {
    await goTo(page, "/models");

    // 列表加载并渲染 2 项（以 alias 为标识）
    await expect(page.getByText("gpt-4o", { exact: true })).toBeVisible();
    await expect(page.getByText("claude-sonnet", { exact: true })).toBeVisible();
    // provider badge 与状态 badge 可见
    await expect(page.getByText("openai", { exact: true })).toBeVisible();
    await expect(page.getByText("enabled", { exact: true })).toBeVisible();
  });

  test("create model config", async ({ page }) => {
    await goTo(page, "/models");

    // 初始 2 项
    await expect(page.getByText("gpt-4o", { exact: true })).toBeVisible();

    // 打开创建表单
    await page.getByRole("button", { name: "+ New Model" }).click();
    await expect(page.getByText("Create Model Config", { exact: true })).toBeVisible();

    // 填写 alias 与 model id（provider/routing 保持默认）
    await page.getByPlaceholder("gpt-4o-mini", { exact: true }).fill("llama-70b");
    await page
      .getByPlaceholder("gpt-4o-mini-2024-07-18", { exact: true })
      .fill("llama-3.1-70b");

    // mock 创建端点（POST 201）
    const created = {
      id: 3,
      alias: "llama-70b",
      provider_name: "openai",
      model_id: "llama-3.1-70b",
      temperature: 0.7,
      max_tokens: 4096,
      cost_per_1k_input: 0,
      cost_per_1k_output: 0,
      routing_strategy: "direct",
      fallback_models: [] as string[],
      quota_daily: 1000000,
      enabled: true,
      created_at: "2026-06-30T00:00:00Z",
    };
    await mockApiMethod(page, "POST", "/api/v1/models", created, 201);

    // 提交并等待 POST 响应（避免误捕初始 GET）
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/api/v1/models") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: "Create", exact: true }).click(),
    ]);
    expect(response.status()).toBe(201);

    // ModelList 在创建后直接 append（不 refetch），新 alias 出现在列表
    await expect(page.getByText("llama-70b", { exact: true })).toBeVisible();
  });
});
