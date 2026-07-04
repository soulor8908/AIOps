"""成本感知模型路由（P1-6）— 按任务复杂度路由 + token budget 熔断。

设计要点：
- ``classify_complexity``：启发式复杂度分类（input 长度 / 工具数 / 代码关键词），
  返回 SIMPLE / MODERATE / COMPLEX。无需 LLM 调用，零成本。
- ``ModelRouter``：按复杂度映射到 cheap / default / premium 模型 alias。
  budget 熔断时一律路由到 cheapest alias。
- ``BudgetTracker``：滑动窗口 token 预算跟踪（内存实现，生产可换 Redis）。
  ``consume`` 记录用量，``remaining`` 返回剩余额度，``is_exhausted`` 判熔断。
- ``execute_agent`` 启用后用路由结果覆盖 ``agent.model_alias``；熔断时记日志。
- 所有路由失败降级为原 agent.model_alias（不阻塞主流程）。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from enum import StrEnum
from typing import Any

logger = logging.getLogger("app.agents.model_router")

# 启发式阈值
_SIMPLE_INPUT_MAX_LEN = 200  # 短输入视为简单
_COMPLEX_INPUT_MIN_LEN = 2000  # 长输入视为复杂
_COMPLEX_TOOL_MIN_COUNT = 3  # 多工具视为复杂
_CODE_KEYWORDS = (
    "def ", "class ", "function ", "import ", "```", "async ", "await ",
    "SELECT ", "CREATE TABLE", "git ", "docker ", "kubernetes",
)


class ComplexityLevel(StrEnum):
    """任务复杂度等级。"""

    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


def classify_complexity(
    user_input: str, tools: list[dict[str, Any]] | None = None
) -> ComplexityLevel:
    """启发式复杂度分类。

    判定规则（任一命中即升级）：
    - COMPLEX：input 含代码关键词、或长度 ≥ 阈值、或工具数 ≥ 阈值
    - SIMPLE：input 短且无工具且无代码关键词
    - MODERATE：其余

    无 LLM 调用，零成本。生产可叠加 LLM 分类（默认关闭，成本过高）。
    """
    if not user_input:
        return ComplexityLevel.SIMPLE
    n_tools = len(tools) if tools else 0
    has_code = any(kw in user_input for kw in _CODE_KEYWORDS)
    if has_code or len(user_input) >= _COMPLEX_INPUT_MIN_LEN or n_tools >= _COMPLEX_TOOL_MIN_COUNT:
        return ComplexityLevel.COMPLEX
    if len(user_input) <= _SIMPLE_INPUT_MAX_LEN and n_tools == 0:
        return ComplexityLevel.SIMPLE
    return ComplexityLevel.MODERATE


class BudgetTracker:
    """滑动窗口 token 预算跟踪器（内存实现）。

    记录窗口内每次 consume 的 (timestamp, tokens)，滑出过期项后求和。
    线程不安全——单 Agent executor 事件循环内使用，无需锁。
    生产环境多实例应换 Redis 实现（ZSET + 时间戳分数）。
    """

    def __init__(self, budget: int, window_seconds: float) -> None:
        self._budget = max(0, budget)
        self._window = max(1.0, window_seconds)
        self._events: deque[tuple[float, int]] = deque()

    def consume(self, tokens: int, *, now: float | None = None) -> None:
        """记录一次 token 消耗。"""
        if tokens <= 0:
            return
        ts = now if now is not None else time.monotonic()
        self._evict(ts)
        self._events.append((ts, tokens))

    def remaining(self, *, now: float | None = None) -> int:
        """返回窗口内剩余预算。"""
        ts = now if now is not None else time.monotonic()
        self._evict(ts)
        used = sum(t for _, t in self._events)
        return max(0, self._budget - used)

    def is_exhausted(self, *, now: float | None = None) -> bool:
        """是否熔断（剩余 ≤ 0）。budget=0 视为不限制（永不熔断）。"""
        if self._budget == 0:
            return False
        return self.remaining(now=now) <= 0

    def _evict(self, now: float) -> None:
        """滑出窗口外的事件。"""
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()


class ModelRouter:
    """成本感知模型路由器。

    按复杂度映射到 cheap / default / premium alias。budget 熔断时一律路由到
    cheapest alias。所有映射缺失降级为 default_alias。
    """

    def __init__(
        self,
        *,
        cheap_alias: str,
        default_alias: str,
        premium_alias: str,
        budget: BudgetTracker | None = None,
    ) -> None:
        self._cheap = cheap_alias
        self._default = default_alias
        self._premium = premium_alias
        self._budget = budget

    def route(
        self, user_input: str, tools: list[dict[str, Any]] | None = None
    ) -> tuple[str, ComplexityLevel, bool]:
        """路由到模型 alias。

        返回 (alias, complexity, circuit_broken)。
        circuit_broken=True 表示因预算熔断降级到 cheapest。
        """
        complexity = classify_complexity(user_input, tools)
        # budget 熔断检测
        if self._budget is not None and self._budget.is_exhausted():
            logger.warning(
                "P1-6 token budget 熔断，降级到 cheapest model (complexity=%s)",
                complexity.value,
            )
            return self._cheap, complexity, True
        mapping = {
            ComplexityLevel.SIMPLE: self._cheap,
            ComplexityLevel.MODERATE: self._default,
            ComplexityLevel.COMPLEX: self._premium,
        }
        return mapping[complexity], complexity, False

    def record_usage(self, tokens: int) -> None:
        """记录一次 LLM 调用的 token 用量到 budget 跟踪器。"""
        if self._budget is not None and tokens > 0:
            self._budget.consume(tokens)


__all__ = [
    "BudgetTracker",
    "ComplexityLevel",
    "ModelRouter",
    "classify_complexity",
]
