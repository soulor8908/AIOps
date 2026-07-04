"""内存指标采集与 Prometheus 导出（observability.spec.md§5）。

设计原则（Karpathy Minimalism）：不引入 ``prometheus-client`` 依赖，用标准库
``collections`` + ``threading.Lock`` 实现进程内指标存储，足以支撑单 worker 的
P1 阶段。多 worker 部署时可通过外挂 Prometheus pushgateway 或替换为
``prometheus_client`` 升级，接口契约不变。

支持的指标（observability.spec.md§5.1）：
- ``request_count`` (counter): labels=[method, endpoint, status]
- ``request_latency`` (histogram): labels=[endpoint], buckets ms
- ``llm_tokens`` (counter): labels=[model, direction]
- ``llm_cost`` (counter): labels=[model]
- ``llm_errors`` (counter): labels=[model, error_type]
- ``llm_calls`` (counter): labels=[model]，成功调用计数（P2-9 AI 健康度用）
- ``llm_ttft`` (histogram): labels=[model]，首 token 延迟（P2-9 TTFT 采集）
- ``tool_calls`` (counter): labels=[tool_name]，工具调用计数（P2-9 工具成功率）
- ``tool_errors`` (counter): labels=[tool_name, error_type]，工具失败计数（P2-9 失败模式聚类）

采集不阻塞请求路径（``record_*`` 仅内存写入 + lock，微秒级开销）。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Final

# 延迟 histogram 桶（单位：毫秒），覆盖 5ms ~ 10s，便于 P50/P95/P99 估算。
# 选桶遵循 Prometheus 最佳实践：指数递增 + +Inf 兜底。
_LATENCY_BUCKETS_MS: Final[tuple[float, ...]] = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0,
)
_LATENCY_BUCKETS_WITH_INF: Final[tuple[float, ...]] = _LATENCY_BUCKETS_MS + (float("inf"),)


class _HistogramState:
    """单条 histogram 的累积状态。"""

    __slots__ = ("buckets", "count", "sum")

    def __init__(self) -> None:
        # 每个 bucket 的累计计数（含 <= 该阈值的样本）
        self.buckets: list[int] = [0] * len(_LATENCY_BUCKETS_WITH_INF)
        self.count: int = 0
        self.sum: float = 0.0


class MetricRegistry:
    """进程内指标注册表（线程安全）。

    所有 ``record_*`` 方法仅做内存写入 + lock，不阻塞请求路径。
    ``render_prometheus`` 输出标准 Prometheus exposition format，
    可被 Prometheus scraper 直接抓取。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # counter: key=(name, labels_tuple) -> value
        self._counters: dict[tuple[str, tuple[str, ...]], float] = defaultdict(float)
        # histogram: key=(name, labels_tuple) -> _HistogramState
        self._histograms: dict[tuple[str, tuple[str, ...]], _HistogramState] = (
            defaultdict(_HistogramState)
        )
        # 标签列定义（导出时按序输出，便于阅读）
        self._counter_label_names: dict[str, tuple[str, ...]] = {
            "request_count": ("method", "endpoint", "status"),
            "llm_tokens": ("model", "direction"),
            "llm_cost": ("model",),
            "llm_errors": ("model", "error_type"),
            "llm_calls": ("model",),
            "tool_calls": ("tool_name",),
            "tool_errors": ("tool_name", "error_type"),
            # P0-2：autonomous loop 运行计数，status ∈ {success, failed, timeout}
            "agent_runs": ("agent_name", "status"),
        }
        self._histogram_label_names: dict[str, tuple[str, ...]] = {
            "request_latency": ("endpoint",),
            "llm_ttft": ("model",),
            # P0-2：autonomous loop 单次执行耗时（秒级）
            "agent_run_duration": ("agent_name",),
        }

    # ===================== 记录接口 =====================

    def record_request(
        self, method: str, endpoint: str, status: int, latency_ms: float
    ) -> None:
        """记录单次请求：counter + histogram。

        - ``request_count{method,endpoint,status}`` += 1
        - ``request_latency{endpoint}`` 观察 latency_ms

        HTTP method 保留原始大写（Prometheus 社区惯例），HTTP method 集合有限
        （GET/POST/PUT/DELETE/PATCH/OPTIONS/HEAD），无基数爆炸风险。
        """
        status_str = str(status)
        counter_key = ("request_count", (method, endpoint, status_str))
        hist_key = ("request_latency", (endpoint,))

        with self._lock:
            self._counters[counter_key] += 1.0
            hist = self._histograms[hist_key]
            hist.count += 1
            hist.sum += latency_ms
            for i, threshold in enumerate(_LATENCY_BUCKETS_WITH_INF):
                if latency_ms <= threshold:
                    hist.buckets[i] += 1

    def record_llm_usage(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
    ) -> None:
        """记录 LLM 调用 token 与成本。

        - ``llm_tokens{model,direction="in"}`` += input_tokens
        - ``llm_tokens{model,direction="out"}`` += output_tokens
        - ``llm_cost{model}`` += cost

        负值被忽略：counter 必须单调递增（Prometheus 语义），misconfigured
        ``cost_per_1k_*`` 导致的负 cost 不应破坏单调性。
        """
        in_key = ("llm_tokens", (model, "in"))
        out_key = ("llm_tokens", (model, "out"))
        cost_key = ("llm_cost", (model,))

        with self._lock:
            if input_tokens > 0:
                self._counters[in_key] += input_tokens
            if output_tokens > 0:
                self._counters[out_key] += output_tokens
            if cost > 0:
                self._counters[cost_key] += cost

    def record_llm_error(self, model: str, error_type: str) -> None:
        """记录 LLM 调用失败（observability.spec.md§5.1：错误率可观测）。

        - ``llm_errors{model,error_type}`` += 1

        ``error_type`` 建议值：``retryable_exhausted`` / ``non_retryable`` /
        ``unsupported_provider``。使失败调用不再处于 metrics 盲区。
        """
        key = ("llm_errors", (model, error_type))
        with self._lock:
            self._counters[key] += 1.0

    def record_llm_call(self, model: str) -> None:
        """记录一次成功的 LLM 调用（P2-9 AI 健康度用）。

        - ``llm_calls{model}`` += 1

        与 ``llm_errors`` 配对：错误率 = ``llm_errors`` /
        (``llm_errors`` + ``llm_calls``)。仅记 ``chat`` 成功 return 路径，
        流式成功不在此记（避免与 ``chat`` 双计），保持健康度统计口径一致。
        """
        key = ("llm_calls", (model,))
        with self._lock:
            self._counters[key] += 1.0

    def record_ttft(self, model: str, ttft_ms: float) -> None:
        """P2-9：记录 LLM 首 token 延迟（Time To First Token）。

        - ``llm_ttft{model}`` 观察 ttft_ms

        TTFT 是流式场景的关键体验指标（用户感知"开始响应"的速度），区别于
        总延迟（含完整生成时间）。仅流式调用首个 token 产出时记录。
        """
        hist_key = ("llm_ttft", (model,))
        with self._lock:
            hist = self._histograms[hist_key]
            hist.count += 1
            hist.sum += ttft_ms
            for i, threshold in enumerate(_LATENCY_BUCKETS_WITH_INF):
                if ttft_ms <= threshold:
                    hist.buckets[i] += 1

    def record_tool_call(self, tool_name: str) -> None:
        """P2-9：记录一次工具调用（无论成功失败均计数）。

        - ``tool_calls{tool_name}`` += 1

        与 ``tool_errors`` 配对：工具成功率 = 1 - ``tool_errors`` / ``tool_calls``。
        """
        key = ("tool_calls", (tool_name,))
        with self._lock:
            self._counters[key] += 1.0

    def record_tool_error(self, tool_name: str, error_type: str) -> None:
        """P2-9：记录一次工具调用失败。

        - ``tool_errors{tool_name,error_type}`` += 1

        ``error_type`` 为异常类名（如 ``TimeoutError`` / ``ValueError``），
        用于失败模式聚类——识别哪些工具/哪些错误类型最频繁失败。
        """
        key = ("tool_errors", (tool_name, error_type))
        with self._lock:
            self._counters[key] += 1.0

    def record_agent_run(
        self, agent_name: str, status: str, duration_ms: float
    ) -> None:
        """P0-2：记录一次 autonomous loop agent 执行。

        - ``agent_runs{agent_name,status}`` += 1（status ∈ success/failed/timeout）
        - ``agent_run_duration{agent_name}`` 观察 duration_ms

        与 ``last_run_status`` ORM 字段配对：DB 存最近一次状态，metrics 存累计
        统计用于趋势分析。长任务（>10s）落入 +Inf 桶，count/sum 仍准确。
        """
        counter_key = ("agent_runs", (agent_name, status))
        hist_key = ("agent_run_duration", (agent_name,))
        with self._lock:
            self._counters[counter_key] += 1.0
            hist = self._histograms[hist_key]
            hist.count += 1
            hist.sum += duration_ms
            for i, threshold in enumerate(_LATENCY_BUCKETS_WITH_INF):
                if duration_ms <= threshold:
                    hist.buckets[i] += 1

    def get_counter_sum(self, name: str) -> float:
        """读取某 counter 跨所有 label 组合的累计总和（P2-9 健康度聚合用）。

        用于 ``llm_calls`` / ``llm_errors`` 这类需要全量汇总的场景；
        分标签的明细仍由 ``get_counter`` 读取。
        """
        total = 0.0
        with self._lock:
            for (n, _labels), value in self._counters.items():
                if n == name:
                    total += value
        return total

    def get_counter_label_values(self, name: str) -> list[tuple[str, ...]]:
        """读取某 counter 当前所有 label 组合（P2-9 健康度聚合用）。

        返回 label 元组列表，便于调用方聚合（如求 ``llm_calls`` 跨 model 的
        distinct 模型数）。返回副本，避免外部修改内部状态。
        """
        with self._lock:
            return [labels for (n, labels) in self._counters if n == name]

    def get_counter_top_labels(
        self, name: str, top_n: int = 5
    ) -> list[tuple[tuple[str, ...], float]]:
        """P2-9：读取某 counter 按 value 降序的 top N label 组合。

        用于失败模式聚类：传入 ``tool_errors`` 返回最频繁失败的
        (tool_name, error_type) → count 排行，帮助运维定位高频失败工具与错误类型。
        """
        with self._lock:
            items = [
                (labels, value)
                for (n, labels), value in self._counters.items()
                if n == name
            ]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items[:top_n]

    def get_histogram_avg(self, name: str) -> float:
        """P2-9：读取某 histogram 跨所有 label 的平均值（sum/count）。

        用于 TTFT 健康度：传入 ``llm_ttft`` 返回平均首 token 延迟。
        无样本时返回 0.0。
        """
        with self._lock:
            total_count = 0
            total_sum = 0.0
            for (n, _labels), state in self._histograms.items():
                if n == name:
                    total_count += state.count
                    total_sum += state.sum
        return total_sum / total_count if total_count > 0 else 0.0

    # ===================== 读取接口（便于测试） =====================

    def get_counter(self, name: str, labels: tuple[str, ...]) -> float:
        """读取 counter 当前值（测试用）。"""
        with self._lock:
            return self._counters.get((name, labels), 0.0)

    def get_histogram(self, name: str, labels: tuple[str, ...]) -> _HistogramState:
        """读取 histogram 状态（测试用，返回副本）。"""
        with self._lock:
            state = self._histograms.get((name, labels))
            if state is None:
                return _HistogramState()
            copy = _HistogramState()
            copy.buckets = list(state.buckets)
            copy.count = state.count
            copy.sum = state.sum
            return copy

    def reset(self) -> None:
        """清空所有指标（测试用，生产不应调用）。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    # ===================== Prometheus exposition format =====================

    def render_prometheus(self) -> str:
        """渲染 Prometheus exposition format 文本。

        格式遵循 Prometheus 标准：
        - ``# HELP`` / ``# TYPE`` 元数据行
        - ``metric_name{label="value",...} value``
        - histogram 输出 ``_bucket`` / ``_count`` / ``_sum`` 三组
        """
        lines: list[str] = []

        # Counters
        for name, label_names in self._counter_label_names.items():
            # 按 labels 排序输出，便于稳定阅读
            with self._lock:
                counter_items = sorted(
                    (
                        (labels, value)
                        for (n, labels), value in self._counters.items()
                        if n == name
                    ),
                    key=lambda kv: kv[0],
                )
            if not counter_items:
                continue
            lines.append(f"# HELP {name} counter metric")
            lines.append(f"# TYPE {name} counter")
            for labels, value in counter_items:
                label_str = self._format_labels(label_names, labels)
                lines.append(f"{name}{label_str} {self._format_value(value)}")

        # Histograms
        for name, label_names in self._histogram_label_names.items():
            with self._lock:
                hist_items = sorted(
                    (
                        (labels, state)
                        for (n, labels), state in self._histograms.items()
                        if n == name
                    ),
                    key=lambda kv: kv[0],
                )
                # 复制 state 避免持锁渲染
                snapshots: list[tuple[tuple[str, ...], _HistogramState]] = [
                    (labels, self._snapshot(state)) for labels, state in hist_items
                ]
            if not snapshots:
                continue
            lines.append(f"# HELP {name} request latency histogram (ms)")
            lines.append(f"# TYPE {name} histogram")
            for labels, snap in snapshots:
                for i, threshold in enumerate(_LATENCY_BUCKETS_WITH_INF):
                    le = "+Inf" if threshold == float("inf") else str(threshold)
                    label_str = self._format_labels(
                        label_names + ("le",), labels + (le,)
                    )
                    lines.append(f"{name}_bucket{label_str} {snap.buckets[i]}")
                label_str = self._format_labels(label_names, labels)
                lines.append(f"{name}_count{label_str} {snap.count}")
                lines.append(f"{name}_sum{label_str} {self._format_value(snap.sum)}")

        return "\n".join(lines) + "\n" if lines else ""

    @staticmethod
    def _snapshot(state: _HistogramState) -> _HistogramState:
        """复制 histogram 状态，避免持锁渲染。"""
        copy = _HistogramState()
        copy.buckets = list(state.buckets)
        copy.count = state.count
        copy.sum = state.sum
        return copy

    @staticmethod
    def _format_labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
        """格式化 ``{name1="value1",name2="value2"}``。

        Prometheus exposition format 要求标签值转义 ``\\`` / ``"`` / ``\\n``。
        """
        if not names:
            return ""

        def _escape(v: str) -> str:
            return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        pairs = ",".join(
            f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)
        )
        return "{" + pairs + "}"

    @staticmethod
    def _format_value(value: float) -> str:
        """整数不输出小数点，浮点保留 6 位；NaN/Inf 按 Prometheus 规范输出。"""
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        if value == int(value):
            return str(int(value))
        return f"{value:.6f}"


# 进程级单例。多 worker 部署时每 worker 独立计数，
# 由 Prometheus scraper 汇总（标准做法）。
metrics = MetricRegistry()
