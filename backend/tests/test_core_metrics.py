"""app/core/metrics.py 单元测试 — 内存指标与 Prometheus 导出。

覆盖（observability.spec.md§5）：
- record_request: request_count counter + request_latency histogram
- record_llm_usage: llm_tokens counter (in/out) + llm_cost counter
- render_prometheus: exposition format 输出格式（HELP / TYPE / bucket / count / sum）
- 线程安全（基础验证）
- reset 清空
"""

from __future__ import annotations

import re

import pytest

from app.core.metrics import MetricRegistry, metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    """每个测试前清空全局 metrics，避免相互污染。"""
    metrics.reset()
    yield
    metrics.reset()


# ===================== record_request =====================

def test_record_request_increments_counter() -> None:
    """request_count 按 method/endpoint/status 维度计数。"""
    metrics.record_request("GET", "/health", 200, 5.0)
    metrics.record_request("GET", "/health", 200, 3.0)
    metrics.record_request("POST", "/api/v1/agents", 201, 50.0)

    assert metrics.get_counter("request_count", ("GET", "/health", "200")) == 2.0
    assert metrics.get_counter("request_count", ("POST", "/api/v1/agents", "201")) == 1.0


def test_record_request_records_latency_histogram() -> None:
    """request_latency histogram 记录 count + sum + bucket。"""
    metrics.record_request("GET", "/health", 200, 5.0)
    metrics.record_request("GET", "/health", 200, 150.0)
    metrics.record_request("GET", "/health", 200, 5000.0)

    state = metrics.get_histogram("request_latency", ("/health",))
    assert state.count == 3
    assert state.sum == 5.0 + 150.0 + 5000.0
    # 5ms 样本应落入第一个桶（<=5ms）
    assert state.buckets[0] == 1
    # 150ms 样本应落入 <=250ms 桶（第 6 个桶，索引 5）
    assert state.buckets[5] == 2
    # 5000ms 样本应落入 <=5000ms 桶（倒数第二个，+Inf 之前）
    assert state.buckets[-2] == 3
    # +Inf 桶应等于总数
    assert state.buckets[-1] == 3


def test_record_request_different_endpoints_isolated() -> None:
    """不同 endpoint 的 histogram 独立。"""
    metrics.record_request("GET", "/a", 200, 5.0)
    metrics.record_request("GET", "/b", 200, 100.0)

    state_a = metrics.get_histogram("request_latency", ("/a",))
    state_b = metrics.get_histogram("request_latency", ("/b",))
    assert state_a.count == 1
    assert state_b.count == 1
    assert state_a.sum == 5.0
    assert state_b.sum == 100.0


# ===================== record_llm_usage =====================

def test_record_llm_usage_tokens() -> None:
    """llm_tokens 按 model/direction 维度累加。"""
    metrics.record_llm_usage("gpt-4o", input_tokens=100, output_tokens=50)
    metrics.record_llm_usage("gpt-4o", input_tokens=200, output_tokens=80)

    assert metrics.get_counter("llm_tokens", ("gpt-4o", "in")) == 300
    assert metrics.get_counter("llm_tokens", ("gpt-4o", "out")) == 130


def test_record_llm_usage_cost() -> None:
    """llm_cost 按 model 维度累加。"""
    metrics.record_llm_usage("gpt-4o", cost=0.5)
    metrics.record_llm_usage("gpt-4o", cost=0.25)

    assert metrics.get_counter("llm_cost", ("gpt-4o",)) == pytest.approx(0.75)


def test_record_llm_usage_zero_skipped() -> None:
    """0 值不累加（避免无谓写入）。"""
    metrics.record_llm_usage("gpt-4o")  # 全 0
    assert metrics.get_counter("llm_tokens", ("gpt-4o", "in")) == 0.0
    assert metrics.get_counter("llm_tokens", ("gpt-4o", "out")) == 0.0
    assert metrics.get_counter("llm_cost", ("gpt-4o",)) == 0.0


def test_record_llm_usage_negative_skipped() -> None:
    """负值不累加（counter 必须单调递增）。"""
    metrics.record_llm_usage(
        "gpt-4o", input_tokens=-10, output_tokens=-5, cost=-0.5
    )
    assert metrics.get_counter("llm_tokens", ("gpt-4o", "in")) == 0.0
    assert metrics.get_counter("llm_tokens", ("gpt-4o", "out")) == 0.0
    assert metrics.get_counter("llm_cost", ("gpt-4o",)) == 0.0


# ===================== record_llm_error =====================

def test_record_llm_error_increments_counter() -> None:
    """llm_errors 按 model/error_type 维度计数。"""
    metrics.record_llm_error("gpt-4o", "retryable_exhausted")
    metrics.record_llm_error("gpt-4o", "retryable_exhausted")
    metrics.record_llm_error("gpt-4o", "non_retryable")
    metrics.record_llm_error("claude", "unsupported_provider")

    assert metrics.get_counter("llm_errors", ("gpt-4o", "retryable_exhausted")) == 2.0
    assert metrics.get_counter("llm_errors", ("gpt-4o", "non_retryable")) == 1.0
    assert metrics.get_counter("llm_errors", ("claude", "unsupported_provider")) == 1.0


def test_record_llm_error_in_prometheus() -> None:
    """llm_errors 出现在 Prometheus 导出。"""
    metrics.record_llm_error("gpt-4o", "non_retryable")
    out = metrics.render_prometheus()
    assert "# TYPE llm_errors counter" in out
    assert 'llm_errors{model="gpt-4o",error_type="non_retryable"} 1' in out


# ===================== render_prometheus =====================

def test_render_prometheus_empty() -> None:
    """无指标时输出为空字符串。"""
    out = metrics.render_prometheus()
    assert out == ""


def test_render_prometheus_counter_format() -> None:
    """counter 输出 HELP / TYPE + metric{labels} value。"""
    metrics.record_request("GET", "/health", 200, 5.0)
    out = metrics.render_prometheus()

    assert "# HELP request_count counter metric" in out
    assert "# TYPE request_count counter" in out
    # 匹配 request_count{method="GET",endpoint="/health",status="200"} 1
    pattern = r'request_count\{method="GET",endpoint="/health",status="200"\} 1'
    assert re.search(pattern, out), f"未匹配到 counter 行: {out}"


def test_render_prometheus_histogram_format() -> None:
    """histogram 输出 _bucket / _count / _sum。"""
    metrics.record_request("GET", "/health", 200, 5.0)
    out = metrics.render_prometheus()

    assert "# TYPE request_latency histogram" in out
    assert 'request_latency_bucket{endpoint="/health",le="5.0"} 1' in out
    assert 'request_latency_bucket{endpoint="/health",le="+Inf"} 1' in out
    assert 'request_latency_count{endpoint="/health"} 1' in out
    assert 'request_latency_sum{endpoint="/health"} 5' in out


def test_render_prometheus_llm_metrics() -> None:
    """LLM token / cost 指标导出。"""
    metrics.record_llm_usage("gpt-4o", input_tokens=100, output_tokens=50, cost=0.01)
    out = metrics.render_prometheus()

    assert 'llm_tokens{model="gpt-4o",direction="in"} 100' in out
    assert 'llm_tokens{model="gpt-4o",direction="out"} 50' in out
    assert 'llm_cost{model="gpt-4o"} 0.01' in out


def test_render_prometheus_int_value_no_decimal() -> None:
    """整数值不输出小数点。"""
    metrics.record_llm_usage("gpt-4o", input_tokens=100)
    out = metrics.render_prometheus()
    assert 'llm_tokens{model="gpt-4o",direction="in"} 100' in out
    assert "100.0" not in out


# ===================== 隔离性 =====================

def test_metric_registry_isolation() -> None:
    """独立 MetricRegistry 实例互不影响。"""
    other = MetricRegistry()
    metrics.record_request("GET", "/a", 200, 1.0)
    assert other.get_counter("request_count", ("GET", "/a", "200")) == 0.0
