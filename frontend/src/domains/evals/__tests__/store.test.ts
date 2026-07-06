import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type {
  EvalRunOut,
  EvalSampleOut,
} from "@/shared/api/types";

// Mock api client：拦截 api.get/post，避免真实 fetch。
vi.mock("@/shared/api/client", () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { api } from "@/shared/api/client";
import { useEvalStore } from "../store";

const mockedApi = vi.mocked(api);

function makeRun(overrides: Partial<EvalRunOut> = {}): EvalRunOut {
  return {
    id: "run-1",
    name: "test-run",
    description: null,
    rules: [],
    cases: [],
    judge_type: "llm",
    status: "pending",
    results: null,
    pass_count: 0,
    fail_count: 0,
    score: null,
    baseline_score: null,
    is_regression: false,
    started_at: null,
    finished_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeSample(overrides: Partial<EvalSampleOut> = {}): EvalSampleOut {
  return {
    id: "sample-1",
    agent_id: null,
    workflow_id: null,
    trigger_source: "http",
    input: "test input",
    actual_output: "test output",
    expected_output: null,
    metadata: {},
    sampled_at: "2026-01-01T00:00:00Z",
    judged: false,
    judge_score: null,
    judge_reason: null,
    eval_run_id: null,
    priority: 0,
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useEvalStore - 基础 CRUD", () => {
  it("fetchList 成功填充 runs", async () => {
    const runs = [makeRun({ id: "r1" }), makeRun({ id: "r2" })];
    mockedApi.get.mockResolvedValueOnce(runs);
    const store = useEvalStore();
    await store.fetchList();
    expect(store.runs).toEqual(runs);
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
    expect(mockedApi.get).toHaveBeenCalledWith("/evals?limit=50&offset=0");
  });

  it("fetchList 失败写 error", async () => {
    mockedApi.get.mockRejectedValueOnce(new Error("network down"));
    const store = useEvalStore();
    await store.fetchList();
    expect(store.error).toBe("network down");
    expect(store.runs).toEqual([]);
    expect(store.loading).toBe(false);
  });

  it("create 成功插入 runs 头部", async () => {
    const newRun = makeRun({ id: "new" });
    mockedApi.post.mockResolvedValueOnce(newRun);
    const store = useEvalStore();
    store.runs = [makeRun({ id: "old" })];
    const result = await store.create({
      name: "x",
      cases: [{ input: "c1" }],
    });
    expect(result).toEqual(newRun);
    expect(store.runs[0]).toEqual(newRun);
    expect(store.runs).toHaveLength(2);
  });

  it("execute 成功更新对应 run", async () => {
    const updated = makeRun({ id: "r1", status: "passed", score: 0.95 });
    mockedApi.post.mockResolvedValueOnce(updated);
    const store = useEvalStore();
    store.runs = [makeRun({ id: "r1", status: "pending" })];
    await store.execute("r1");
    expect(store.runs[0]).toEqual(updated);
    expect(store.running).toBe(false);
  });
});

describe("useEvalStore - E2 online eval 闭环", () => {
  it("fetchSamples 默认查询未 judged 样本", async () => {
    const samples = [
      makeSample({ id: "s1", priority: 2 }),
      makeSample({ id: "s2", priority: 0, judged: true }),
    ];
    mockedApi.get.mockResolvedValueOnce(samples);
    const store = useEvalStore();
    await store.fetchSamples();
    expect(store.samples).toEqual(samples);
    expect(store.samplesLoading).toBe(false);
    // 默认 judged=false
    expect(mockedApi.get).toHaveBeenCalledWith("/evals/samples?judged=false");
  });

  it("fetchSamples 支持自定义 query", async () => {
    mockedApi.get.mockResolvedValueOnce([]);
    const store = useEvalStore();
    await store.fetchSamples({ judged: true, priority_min: 1, limit: 50 });
    expect(mockedApi.get).toHaveBeenCalledWith(
      "/evals/samples?judged=true&priority_min=1&limit=50",
    );
  });

  it("fetchSamples 失败写 error", async () => {
    mockedApi.get.mockRejectedValueOnce(new Error("500"));
    const store = useEvalStore();
    await store.fetchSamples();
    expect(store.error).toBe("500");
    expect(store.samples).toEqual([]);
  });

  it("runOnlineEval 成功插入 run 头部 + 刷新 samples + 清空选择", async () => {
    const newRun = makeRun({ id: "online-1", is_regression: true, baseline_score: 0.8 });
    // runOnlineEval 内部会调 fetchSamples 刷新
    const refreshedSamples = [makeSample({ id: "s2" })];
    mockedApi.post.mockResolvedValueOnce(newRun); // runOnlineEval
    mockedApi.get.mockResolvedValueOnce(refreshedSamples); // fetchSamples

    const store = useEvalStore();
    store.runs = [makeRun({ id: "old" })];
    store.selectedSampleIds = new Set(["s1"]);

    const result = await store.runOnlineEval({
      golden_run_name: "golden-v1",
      sample_ids: ["s1"],
    });

    expect(result).toEqual(newRun);
    expect(store.runs[0]).toEqual(newRun);
    expect(store.onlineEvalRunning).toBe(false);
    // 选择被清空
    expect(store.selectedSampleIds.size).toBe(0);
    // samples 被刷新
    expect(store.samples).toEqual(refreshedSamples);
    // 调用了 online-eval 端点
    expect(mockedApi.post).toHaveBeenCalledWith("/evals/online-eval", {
      golden_run_name: "golden-v1",
      sample_ids: ["s1"],
    });
  });

  it("runOnlineEval 失败写 error + 抛出", async () => {
    mockedApi.post.mockRejectedValueOnce(new Error("forbidden"));
    const store = useEvalStore();
    await expect(
      store.runOnlineEval({ golden_run_name: "g1" }),
    ).rejects.toThrow("forbidden");
    expect(store.error).toBe("forbidden");
    expect(store.onlineEvalRunning).toBe(false);
  });

  it("toggleSampleSelection 切换选中状态", () => {
    const store = useEvalStore();
    expect(store.selectedSampleIds.has("s1")).toBe(false);
    store.toggleSampleSelection("s1");
    expect(store.selectedSampleIds.has("s1")).toBe(true);
    store.toggleSampleSelection("s1");
    expect(store.selectedSampleIds.has("s1")).toBe(false);
  });

  it("selectAllPendingSamples 仅选未 judged", () => {
    const store = useEvalStore();
    store.samples = [
      makeSample({ id: "s1", judged: false }),
      makeSample({ id: "s2", judged: false }),
      makeSample({ id: "s3", judged: true }),
    ];
    store.selectAllPendingSamples();
    expect(store.selectedSampleIds.size).toBe(2);
    expect(store.selectedSampleIds.has("s1")).toBe(true);
    expect(store.selectedSampleIds.has("s2")).toBe(true);
    expect(store.selectedSampleIds.has("s3")).toBe(false);
  });

  it("clearSampleSelection 清空", () => {
    const store = useEvalStore();
    store.selectedSampleIds = new Set(["s1", "s2"]);
    store.clearSampleSelection();
    expect(store.selectedSampleIds.size).toBe(0);
  });
});
