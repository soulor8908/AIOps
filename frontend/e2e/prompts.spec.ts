import { test, expect, goTo, login, type Page } from "./fixtures";

/**
 * Prompt Studio 关键路径（L3 E2E）。
 *
 * 使用有状态的路由 mock 模拟后端：GET 列表支持 q 过滤、POST 创建会把新 prompt
 * 加入列表，从而覆盖 PromptList 在创建后 refetch 的真实行为（store.create 先
 * prepend，随后 onCreated 再次 fetchList，因此 GET 必须返回包含新项的列表）。
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

/** 确定性 UUID：最后一段用 n 的零填充，其余段固定为占位 UUID。 */
function uuidFromIndex(n: number): string {
  return `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
}

function mkPrompt(
  index: number,
  name: string,
  description: string,
  content: string,
  variables: string[] = [],
): Prompt {
  const id = uuidFromIndex(index);
  const versionId = uuidFromIndex(index * 10 + 1);
  return {
    id,
    name,
    description,
    current_version_id: versionId,
    is_active: true,
    created_at: "2026-06-29T10:00:00Z",
    updated_at: "2026-06-29T10:00:00Z",
    versions: [
      {
        id: versionId,
        prompt_id: id,
        version_num: 1,
        content,
        variables,
        change_note: null,
        created_by: null,
        created_at: "2026-06-29T10:00:00Z",
      },
    ],
  };
}

const SEED: Prompt[] = [
  mkPrompt(1, "Greeting Prompt", "Greets the user", "Hello {{name}}", ["name"]),
  mkPrompt(2, "Summary Prompt", "Summarizes text", "Summarize: {{text}}", ["text"]),
];

/** 注册有状态的 Prompts API mock：列表(带 q 过滤) + 创建 + 版本列表。 */
async function mockPromptsApi(page: Page, seed: Prompt[] = SEED): Promise<void> {
  const items: Prompt[] = [...seed];
  let nextIndex = seed.length + 1;

  // 列表 + 创建（同一 path，按方法分发）
  await page.route(/\/api\/v1\/prompts(\?.*)?$/, async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      const url = new URL(route.request().url());
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const filtered = q
        ? items.filter((p) => p.name.toLowerCase().includes(q))
        : items;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(filtered),
      });
    }
    if (method === "POST") {
      const body = route.request().postDataJSON() as {
        name: string;
        content: string;
        description?: string;
      };
      const created: Prompt = mkPrompt(nextIndex++, body.name, body.description ?? "", body.content);
      created.updated_at = "2026-06-30T00:00:00Z";
      created.versions = [
        {
          ...created.versions[0],
          content: body.content,
          variables: [],
          created_at: "2026-06-30T00:00:00Z",
        },
      ];
      created.current_version_id = created.versions[0].id;
      items.unshift(created);
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(created),
      });
    }
    return route.fallback();
  });

  // 版本列表（选中 prompt 时拉取，独立 path，不会与列表路由冲突）
  await page.route(/\/api\/v1\/prompts\/[0-9a-f-]+\/versions(\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    }),
  );
}

test.beforeEach(async ({ page }) => {
  await login(page);
  await mockPromptsApi(page);
});

test.describe("Prompt Studio — 关键路径", () => {
  test("create new prompt", async ({ page }) => {
    await goTo(page, "/prompts");

    // 列表已加载 2 项
    await expect(page.getByText("Greeting Prompt", { exact: true })).toBeVisible();
    await expect(page.getByText("Summary Prompt", { exact: true })).toBeVisible();

    // 打开创建表单
    await page.getByRole("button", { name: "+ New" }).click();
    await expect(page.getByPlaceholder("prompt-name")).toBeVisible();

    // 填写表单
    await page.getByPlaceholder("prompt-name").fill("My New Prompt");
    await page.getByPlaceholder("You are a helpful assistant...").fill("Answer: {{q}}");

    // 提交并等待 POST 响应（避免误捕初始 GET）
    const [response] = await Promise.all([
      page.waitForResponse(
        (r) =>
          r.url().includes("/api/v1/prompts") &&
          r.request().method() === "POST" &&
          r.ok(),
      ),
      page.getByRole("button", { name: "Create Prompt" }).click(),
    ]);
    expect(response.status()).toBe(201);

    // 创建后 PromptList 会 refetch；状态化 mock 已将新 prompt 纳入列表
    await expect(page.getByText("My New Prompt", { exact: true })).toBeVisible();
  });

  test("search prompts", async ({ page }) => {
    await goTo(page, "/prompts");

    await expect(page.getByText("Greeting Prompt", { exact: true })).toBeVisible();
    await expect(page.getByText("Summary Prompt", { exact: true })).toBeVisible();

    // 输入搜索词并提交（服务端按 q 过滤）
    await page.getByPlaceholder("Search prompts...").fill("Greeting");
    await page.getByRole("button", { name: "Search" }).click();

    // 仅 Greeting Prompt 可见，Summary Prompt 被过滤掉
    await expect(page.getByText("Greeting Prompt", { exact: true })).toBeVisible();
    await expect(page.getByText("Summary Prompt", { exact: true })).toHaveCount(0);

    // 清空搜索恢复全部
    await page.getByPlaceholder("Search prompts...").clear();
    await page.getByRole("button", { name: "Search" }).click();
    await expect(page.getByText("Summary Prompt", { exact: true })).toBeVisible();
  });

  test("view prompt detail", async ({ page }) => {
    await goTo(page, "/prompts");

    // 点击列表首项
    await page.getByText("Greeting Prompt", { exact: true }).click();

    // 详情面板显示：未选中提示消失 + 当前版本内容可见
    await expect(page.getByText("Select a prompt to view details.")).toHaveCount(0);
    await expect(page.getByText("Hello {{name}}", { exact: true })).toBeVisible();
    // 版本卡片标题（versions 列表已 mock 为空）
    await expect(page.getByText(/Versions \(\d+\)/)).toBeVisible();
  });
});
