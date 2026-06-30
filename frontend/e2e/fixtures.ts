import { test as base, expect, type APIResponse, type Page } from "@playwright/test";

export { expect };
export const test = base;

/** Mock JWT token 写入 localStorage，模拟已登录会话。 */
export const MOCK_TOKEN = "e2e-mock-jwt-token";

/** 应用读取 auth token 的 localStorage key（与 shared/stores/user 对齐）。 */
export const TOKEN_KEY = "token";

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
 * 登录 helper（mock auth）。
 * 在页面首次加载前向 localStorage 写入 mock token，使应用以已认证状态启动。
 * 后续所有导航/重载都会复用该 init script。
 */
export async function login(page: Page, token: string = MOCK_TOKEN): Promise<void> {
  await page.addInitScript((t: string) => {
    window.localStorage.setItem(TOKEN_KEY, t);
  }, token);
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
