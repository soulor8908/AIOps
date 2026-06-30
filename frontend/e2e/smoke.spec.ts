import { test, expect, goTo, login, type Page } from "./fixtures";

/**
 * 全部导航项（与 App.vue 的 navItems 对齐）。
 * RouterLink 渲染为 <a>，其 accessible name 取自 icon span + label span
 * （如 "M Dashboard"），因此用子串匹配而非精确字符串。
 */
const NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/", label: "Dashboard" },
  { to: "/prompts", label: "Prompt Studio" },
  { to: "/agents", label: "Agent Orchestrator" },
  { to: "/knowledge", label: "Knowledge Base" },
  { to: "/models", label: "Model Router" },
  { to: "/analytics", label: "Analytics" },
  { to: "/evals", label: "Eval Suite" },
];

/** 注册兜底 API mock：所有 /api/v1 GET 返回空列表，避免视图加载时网络报错。 */
async function mockAllApi(page: Page): Promise<void> {
  await page.route(/\/api\/v1\/.*/, (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0 }),
      });
    }
    return route.fallback();
  });
}

test.beforeEach(async ({ page }) => {
  // 每个用例都以 mock 登录态启动，并兜底拦截 API 请求
  await login(page);
  await mockAllApi(page);
});

test.describe("Smoke — 基本页面加载", () => {
  test("homepage loads and shows dashboard", async ({ page }) => {
    await goTo(page, "/");

    // document.title 经路由 afterEach 设置为 "Dashboard - AIOps Console"
    await expect(page).toHaveTitle(/Dashboard/);
    // 仪表盘视图标题（main 内的 h1，区别于 header 内的 "AIOps Console"）
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    // 顶部 header 标题
    await expect(page.locator("header h1")).toHaveText("AIOps Console");
    // 侧边栏 Dashboard 导航项可见
    await expect(page.getByRole("link", { name: "Dashboard" })).toBeVisible();
    // 主内容区可见
    await expect(page.locator("main")).toBeVisible();
  });

  test("navigation to prompts page works", async ({ page }) => {
    await goTo(page, "/");

    await page.getByRole("link", { name: "Prompt Studio" }).click();

    await expect(page).toHaveURL(/\/prompts$/);
    await expect(page).toHaveTitle(/Prompt Studio/);
    // 页面标题加载，确认内容已渲染
    await expect(page.getByRole("heading", { name: "Prompt Studio" })).toBeVisible();
    await expect(page.locator("main")).toBeVisible();
  });

  test("navigation to all pages", async ({ page }) => {
    await goTo(page, "/");

    for (const item of NAV_ITEMS) {
      await page.getByRole("link", { name: item.label }).click();
      // 校验 URL（"/" 单独处理为结尾斜杠）
      const urlRe = new RegExp(item.to === "/" ? "/$" : item.to + "$");
      await expect(page).toHaveURL(urlRe);
      // 校验路由 meta title
      await expect(page).toHaveTitle(new RegExp(item.label));
      await expect(page.locator("main")).toBeVisible();
    }
  });

  test("sidebar toggle works", async ({ page }) => {
    await goTo(page, "/");

    const aside = page.locator("aside");
    // 展开态：侧边栏宽（w-64），Dashboard 文本标签可见
    const dashboardLabelLink = page.locator("nav a", { hasText: "Dashboard" });
    await expect(aside).toHaveClass(/w-64/);
    await expect(dashboardLabelLink).toBeVisible();

    const toggle = aside.getByRole("button");
    await toggle.click();

    // 折叠态：侧边栏窄（w-16），文本标签被 v-if 移除，指示符变为 ">"
    await expect(aside).toHaveClass(/w-16/);
    await expect(dashboardLabelLink).toHaveCount(0);
    await expect(toggle).toContainText(">");

    // 再次点击恢复展开
    await toggle.click();
    await expect(aside).toHaveClass(/w-64/);
    await expect(dashboardLabelLink).toBeVisible();
  });
});
