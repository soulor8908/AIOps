import { describe, expect, it } from "vitest";
import {
  buildQuery,
  cn,
  formatBytes,
  formatCost,
  formatDate,
  formatNumber,
  formatPercent,
} from "../index";

describe("formatDate", () => {
  it("格式化 ISO 字符串为包含年份的本地化字符串", () => {
    const out = formatDate("2026-06-30T10:20:00Z");
    expect(out).toMatch(/2026/);
    expect(out).not.toBe("-");
  });

  it("接受 Date 对象", () => {
    const out = formatDate(new Date("2026-01-02T03:04:05Z"));
    expect(out).toMatch(/2026/);
  });

  it("空值与非法日期返回占位符 \"-\"", () => {
    expect(formatDate("")).toBe("-");
    expect(formatDate(null)).toBe("-");
    expect(formatDate(undefined)).toBe("-");
    expect(formatDate("not-a-date")).toBe("-");
  });
});

describe("formatNumber", () => {
  it("添加千分位分隔", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
  });

  it("按指定小数位格式化", () => {
    expect(formatNumber(1.2345, 2)).toBe("1.23");
  });

  it("零也正常格式化", () => {
    expect(formatNumber(0)).toBe("0");
  });

  it("null/undefined/NaN 返回占位符 \"-\"", () => {
    expect(formatNumber(null)).toBe("-");
    expect(formatNumber(undefined)).toBe("-");
    expect(formatNumber(Number.NaN)).toBe("-");
  });
});

describe("formatCost", () => {
  it("格式化为美元并固定 4 位小数", () => {
    expect(formatCost(12.5)).toBe("$12.5000");
  });

  it("四舍五入到 4 位小数", () => {
    expect(formatCost(0.123456)).toBe("$0.1235");
  });

  it("null/undefined/NaN 返回占位符 \"-\"", () => {
    expect(formatCost(null)).toBe("-");
    expect(formatCost(undefined)).toBe("-");
    expect(formatCost(Number.NaN)).toBe("-");
  });
});

describe("formatBytes", () => {
  it("0 字节返回 \"0 B\"", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  it("1024 字节格式化为 KB（固定 1 位小数）", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
  });

  it("1048576 字节格式化为 MB", () => {
    expect(formatBytes(1048576)).toBe("1.0 MB");
  });

  it("超过 TB 时停留在最大单位 TB", () => {
    const huge = Math.pow(1024, 5); // 1 PiB -> idx 被 clamp 到 TB
    expect(formatBytes(huge)).toMatch(/TB$/);
  });

  it("null/undefined/NaN 返回占位符 \"-\"", () => {
    expect(formatBytes(null)).toBe("-");
    expect(formatBytes(undefined)).toBe("-");
    expect(formatBytes(Number.NaN)).toBe("-");
  });
});

describe("formatPercent", () => {
  it("将 0-1 比率格式化为百分比（1 位小数）", () => {
    expect(formatPercent(0.875)).toBe("87.5%");
    expect(formatPercent(0.5)).toBe("50.0%");
  });

  it("1 对应 100%", () => {
    expect(formatPercent(1)).toBe("100.0%");
  });

  it("null/undefined/NaN 返回占位符 \"-\"", () => {
    expect(formatPercent(null)).toBe("-");
    expect(formatPercent(undefined)).toBe("-");
    expect(formatPercent(Number.NaN)).toBe("-");
  });
});

describe("cn", () => {
  it("合并多个真值类名", () => {
    expect(cn("a", "b", "c")).toBe("a b c");
  });

  it("丢弃 falsy 值（false/null/undefined/空串）", () => {
    expect(cn("a", false, null, undefined, "", "b")).toBe("a b");
  });

  it("无入参时返回空字符串", () => {
    expect(cn()).toBe("");
  });
});

describe("buildQuery", () => {
  it("跳过 undefined 与空串参数", () => {
    expect(buildQuery({ q: "", limit: 20, offset: 0 })).toBe("?limit=20&offset=0");
  });

  it("全部为空时返回空串", () => {
    expect(buildQuery({ q: "", x: undefined })).toBe("");
  });
});
