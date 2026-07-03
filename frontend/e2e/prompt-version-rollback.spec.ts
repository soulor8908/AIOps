import { test, expect, goTo, login, type Page } from "./fixtures";

/**
 * L3 E2E 关键路径（testing.spec.md §5.1）：
 * 登录 → 创建 Prompt → 版本管理（新增版本）→ 回滚到上一版本。
 *
 * 使用有状态路由 mock 模拟后端版本端点：
 * - GET  /prompts                  列表
 * - POST /prompts                  创建 prompt（含初始 v1）
 * - GET  /prompts/:id/versions     版本列表
 * - POST /prompts/:id/versions     新增版本
 * - POST /prompts/:id/versions/:vid/rollback  回滚
 *
 * store 行为：createVersion 后本地 prepend（不 refetch）；
 * rollback 后先 POST 再 GET /versions 复刷列表。mock 据此维护内存态。
 */

interface PromptVersion {
  id: string;
  prompt_id: string;
  version_num: number;
  content: string;
  variables: string[];
  change_note: string | null;
  created_by: string | null;
  created_at: string;
}

interface Prompt {
  id: string;
  name: string;
  description: string;
  current_version_id: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  versions: PromptVersion[];
}

/** 确定性 UUID：最后一段用 n 的零填充。 */
function uuidFromIndex(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

const NOW = "2026-07-03T10:00:00Z";

/**
 * 注册有状态 Prompts + Versions API mock。
 * 返回创建的 seed prompt id，供测试引用。
 */
async function mockVersionedPromptsApi(page: Page): Promise<string> {
  // 内存态：prompt + 关联 versions
  const promptId = uuidFromIndex(1);
  const v1: PromptVersion = {
    id: uuidFromIndex(11),
    prompt_id: promptId,
    version_num: 1,
    content: "Initial content v1",
    variables: [],
    change_note: null,
    created_by: null,
    created_at: NOW,
  };
  const seedPrompt: Prompt = {
    id: promptId,
    name: "Rollback Test Prompt",
    description: "For version management E2E",
    current_version_id: v1.id,
    is_active: true,
    created_at: NOW,
    updated_at: NOW,
    versions: [v1],
  };
  const prompts: Prompt[] = [seedPrompt];
  let nextId = 100;

  function newUuid(): string {
    return uuidFromIndex(nextId++);
  }

  // 列表 + 创建 prompt
  await page.route(/\/api\/v1\/prompts(\?.*)?$/, async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(prompts),
      });
    }
    if (method === "POST") {
      const body = route.request().postDataJSON() as {
        name: string;
        content: string;
        description?: string;
      };
      const pid = newUuid();
      const vid = newUuid();
      const initVer: PromptVersion = {
        id: vid,
        prompt_id: pid,
        version_num: 1,
        content: body.content,
        variables: [],
        change_note: null,
        created_by: null,
        created_at: NOW,
      };
      const created: Prompt = {
        id: pid,
        name: body.name,
        description: body.description ?? "",
        current_version_id: vid,
        is_active: true,
        created_at: NOW,
        updated_at: NOW,
        versions: [initVer],
      };
      prompts.unshift(created);
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
    }
    return route.fallback();
  });

  // 版本列表 + 新增版本（同一 path，按方法分发）
  await page.route(/\/api\/v1\/prompts\/[0-9a-f-]+\/versions(\?.*)?$/, async (route) => {
    const url = route.request().url();
    const method = route.request().method();
    // 从 URL 提取 prompt id
    const match = url.match(/\/api\/v1\/prompts\/([0-9a-f-]+)\/versions/);
    const pid = match ? match[1] : "";
    const prompt = prompts.find((p) => p.id === pid);

    if (method === "GET") {
      const versions = prompt?.versions ?? [];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(versions),
      });
    }
    if (method === "POST") {
      const body = route.request().postDataJSON() as { content: string; variables?: string[] };
      const newVer: PromptVersion = {
        id: newUuid(),
        prompt_id: pid,
        version_num: (prompt?.versions.length ?? 0) + 1,
        content: body.content,
        variables: body.variables ?? [],
        change_note: null,
        created_by: null,
        created_at: NOW,
      };
      if (prompt) {
        prompt.versions.unshift(newVer);
        prompt.current_version_id = newVer.id;
        prompt.updated_at = NOW;
      }
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(newVer),
      });
    }
    return route.fallback();
  });

  // 回滚：POST /prompts/:id/versions/:vid/rollback → 返回目标版本
  await page.route(
    /\/api\/v1\/prompts\/[0-9a-f-]+\/versions\/[0-9a-f-]+\/rollback$/,
    async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const url = route.request().url();
      const match = url.match(/\/api\/v1\/prompts\/([0-9a-f-]+)\/versions\/([0-9a-f-]+)\/rollback/);
      const pid = match ? match[1] : "";
      const vid = match ? match[2] : "";
      const prompt = prompts.find((p) => p.id === pid);
      const target = prompt?.versions.find((v) => v.id === vid);
      if (prompt && target) {
        prompt.current_version_id = target.id;
        prompt.updated_at = NOW;
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(target ?? {}),
      });
    },
  );

  return promptId;
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await mockVersionedPromptsApi(page);
  // 自动接受 confirm() 对话框（回滚前的前端确认）
  page.on("dialog", (dialog) => dialog.accept());
});

test.describe("Prompt 版本管理与回滚（L3 关键路径）", () => {
  test("新增版本后回滚到上一版本", async ({ page }) => {
    await goTo(page, "/prompts");

    // 选中 seed prompt 进入详情
    await page.getByText("Rollback Test Prompt", { exact: true }).click();
    await expect(page.getByText("Initial content v1", { exact: true })).toBeVisible();

    // 初始：仅 v1，标记为 current，无 Rollback 按钮（current 版本不显示 Rollback）
    await expect(page.getByText("v1")).toBeVisible();
    await expect(page.getByText("current", { exact: true })).toBeVisible();

    // 新增版本 v2
    await page.getByRole("button", { name: "+ New Version" }).click();
    await page.getByPlaceholder("Enter new prompt content...").fill("Updated content v2");

    const [versionResp] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/versions") &&
          !r.url().includes("rollback") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: "Save Version" }).click(),
    ]);
    expect(versionResp.status()).toBe(201);

    // v2 成为 current，内容更新
    await expect(page.getByText("Updated content v2", { exact: true })).toBeVisible();
    await expect(page.getByText("Versions (2)")).toBeVisible();

    // 回滚到 v1：点击 v1 的 Rollback 按钮
    const [rollbackResp] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes("/rollback") && r.request().method() === "POST" && r.ok(),
      ),
      page.getByRole("button", { name: "Rollback" }).click(),
    ]);
    expect(rollbackResp.status()).toBe(200);

    // 回滚后 v1 重新成为 current，内容恢复
    await expect(page.getByText("Initial content v1", { exact: true })).toBeVisible();
    // v2 现在非 current，应显示 Rollback 按钮
    await expect(page.getByRole("button", { name: "Rollback" })).toBeVisible();
  });

  test("创建新 prompt 后管理其版本", async ({ page }) => {
    await goTo(page, "/prompts");

    // 创建新 prompt
    await page.getByRole("button", { name: "+ New" }).click();
    await page.getByPlaceholder("prompt-name").fill("Fresh Prompt");
    await page.getByPlaceholder("You are a helpful assistant...").fill("Base content");

    await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes("/api/v1/prompts") && r.request().method() === "POST" && r.ok(),
      ),
      page.getByRole("button", { name: "Create Prompt" }).click(),
    ]);

    // 选中刚创建的 prompt
    await page.getByText("Fresh Prompt", { exact: true }).click();
    await expect(page.getByText("Base content", { exact: true })).toBeVisible();
    await expect(page.getByText("v1")).toBeVisible();

    // 新增第二个版本
    await page.getByRole("button", { name: "+ New Version" }).click();
    await page.getByPlaceholder("Enter new prompt content...").fill("Revised content");

    await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/versions") &&
          !r.url().includes("rollback") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: "Save Version" }).click(),
    ]);

    await expect(page.getByText("Revised content", { exact: true })).toBeVisible();
    await expect(page.getByText("Versions (2)")).toBeVisible();

    // 回滚到 v1
    await page.getByRole("button", { name: "Rollback" }).click();
    await expect(page.getByText("Base content", { exact: true })).toBeVisible();
  });
});
