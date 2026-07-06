import { test as base, expect, type APIResponse, type Page } from "@playwright/test";

export { expect };
export const test = base;
export type { APIResponse, Page };

/** 需要在正则中转义的特殊字符（反斜杠必须最先处理）。 */
const REGEX_SPECIAL = [
  "\\",
  ".",
  "*",
  "+",
  "?",
  "^",
  "$",
  "{",
  "}",
  "(",
  ")",
  "|",
  "[",
  "]",
  "/",
];

/** 转义字符串中的正则元字符（用 split/join 实现，避免使用正则字面量）。 */
function escapeRegex(s: string): string {
  let out = s;
  for (const ch of REGEX_SPECIAL) {
    out = out.split(ch).join("\\" + ch);
  }
  return out;
}

/**
 * 将 API path 转为「路径 + 可选查询串」的正则。
 * 使用正则而非 glob，可正确匹配带 query string 的 URL
 * （如 /api/v1/prompts?limit=20&offset=0），避免 glob 通配对 query 失配。
 */
function pathToRegExp(path: string): RegExp {
  return new RegExp(escapeRegex(path) + "(\\?.*)?$");
}

/**
 * E2E 默认 mock 用户（与后端 UserOut 对齐，缺少的字段由 store normalize 补全）。
 *
 * Batch 6c：cookie 模式下前端不再持有 token 明文，router boot 首航会调
 * /auth/me 探测 cookie 是否有效。E2E 没有 httpOnly cookie 通道（无真实后端），
 * 改为 mock /auth/me 返回 200 + 用户对象，使应用以已认证状态启动。
 */
const MOCK_USER = {
  id: "e2e-mock-user-00000000-0000-4000-8000-000000000001",
  email: "e2e@test.local",
  username: "e2e_user",
  role: "user",
  is_active: true,
  created_at: "2026-06-29T10:00:00Z",
};

/**
 * 登录 helper（mock auth）。
 *
 * Batch 6c：cookie 模式——不再向 localStorage 写 token（前端已不读 localStorage）。
 * 改为注册 /auth/me GET 路由 mock，返回 200 + MOCK_USER，使 router boot 首航
 * fetchMe 成功 → isAuthenticated=true → 路由守卫放行 requiresAuth 页面。
 *
 * 必须在 page.goto 之前调用（router beforeEach 在首航触发 fetchMe）。
 * 后续导航/重载复用同一 route mock（page.route 在 page 生命周期内持续生效）。
 */
export async function login(page: Page): Promise<void> {
  await page.route(
    /\/api\/v1\/auth\/me(\?.*)?$/,
    (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(MOCK_USER),
        });
      }
      return route.fallback();
    },
  );
}

/**
 * 导航 helper：访问指定路径并等待主内容区可见。
 */
export async function goTo(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.locator("main")).toBeVisible();
}

/**
 * 等待 API 响应 helper：等待 URL 匹配 urlPattern 且状态码成功的响应。
 */
export async function waitForApiResponse(
  page: Page,
  urlPattern: string | RegExp,
  timeout = 15_000,
): Promise<APIResponse> {
  return page.waitForResponse(
    (resp) => {
      const url = resp.url();
      const matched =
        typeof urlPattern === "string" ? url.includes(urlPattern) : urlPattern.test(url);
      return matched && resp.ok();
    },
    { timeout },
  );
}

/** 构造 JSON fulfill 响应体。 */
function jsonFulfill(payload: unknown, status: number) {
  return {
    status,
    contentType: "application/json" as const,
    body: JSON.stringify(payload),
  };
}

/**
 * Mock 一个 JSON 响应（匹配 path 的所有方法均返回同一 payload）。
 */
export async function mockApi(
  page: Page,
  path: string,
  payload: unknown,
  status = 200,
): Promise<void> {
  await page.route(pathToRegExp(path), (route) =>
    route.fulfill(jsonFulfill(payload, status)),
  );
}

/**
 * Mock 指定 HTTP 方法的 JSON 响应；其它方法走 fallback。
 */
export async function mockApiMethod(
  page: Page,
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  payload: unknown,
  status = 200,
): Promise<void> {
  await page.route(pathToRegExp(path), (route) => {
    if (route.request().method() === method) {
      return route.fulfill(jsonFulfill(payload, status));
    }
    return route.fallback();
  });
}

/** GET 请求 mock 的便捷别名。 */
export const mockApiGet = (
  page: Page,
  path: string,
  payload: unknown,
  status = 200,
): Promise<void> => mockApiMethod(page, "GET", path, payload, status);
