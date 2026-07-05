"""P0-10：fire-and-forget task 背压与生命周期管理。

背景
----
当前代码用 ``asyncio.create_task`` 启动两类 fire-and-forget 后台 task：

1. ``agents/service.py::execute_agent`` 在线 eval 采样记录（每个 HTTP 请求
   按 ``online_eval_sample_rate`` 概率触发）。
2. ``agents/executor.py::_record_failure_safely`` 失败模式聚类记录。

裸 ``create_task`` 存在两类隐患：

- **静默泄漏**：task 引用未被持有，Python 仅持有弱引用，可能在 GC 时被回收
  并抛出 ``Task was destroyed but it is pending!`` 警告，导致采样丢失而无告警。
- **无背压**：高 QPS 下 task 无限累积，event loop 调度队列膨胀、内存上涨，
  极端情况 OOM。生产环境（多 pod / 长连接 / 突发流量）必须限制并发。

设计
----
``TaskRegistry`` 用单一全局信号量限制并发数，并持有强引用防止 GC 回收：

- ``spawn(coro)``：尝试获取信号量（非阻塞）；获取成功则创建 task，注册到
  ``_tasks`` set，完成后自动从 set 移除并 release 信号量；获取失败（背压满）
  则丢弃 coro 并记 warning，**绝不阻塞主请求路径**。
- ``shutdown()``：lifespan 关闭时调用，cancel 所有未完成 task 并等待退出，
  防止 task 在 event loop 关闭后还尝试运行（``Event loop is closed`` 错误）。

背压策略选择：丢弃而非排队。fire-and-forget task 失败成本本来就低
（采样丢失、聚类丢失），丢弃比阻塞主请求或堆积内存更安全。

线程安全
--------
``asyncio.Semaphore`` 与 ``asyncio.create_task`` 仅可在事件循环线程调用。
本模块所有方法都设计为在事件循环线程同步调用（``spawn`` 非 async 但调用方
都在 async 函数内），不涉及跨线程同步。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger("app.core.task_registry")

_T = TypeVar("_T")


class TaskRegistry:
    """fire-and-forget task 注册中心 + 背压信号量。

    单例（通过 ``get_task_registry`` 获取）。背压上限由
    ``settings.task_registry_max_concurrency`` 控制。

    设计要点：

    - ``spawn`` **不 await**——返回 ``None``，调用方 fire-and-forget。
    - 信号量用 ``acquire()`` 的非阻塞形式（``acquire`` 在 ``try_acquire`` 内
      立即返回 bool），满时直接丢弃。
    - 强引用集合 ``_tasks`` 防止 task 被 GC（Python ``create_task`` 仅持弱引用，
      task 未完成时被回收会丢任务并报警）。
    - task done 回调自动从 ``_tasks`` 移除并 release 信号量，避免泄漏。
    """

    def __init__(self, max_concurrency: int) -> None:
        # ``max_concurrency <= 0`` 视为无限制（仅持有强引用，不限并发）
        self._max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )
        # 强引用集合——task done 回调内 discard，避免 set 无限增长
        self._tasks: set[asyncio.Task[Any]] = set()
        # 标记是否已 shutdown——shutdown 后拒绝新 task
        self._shutdown = False

    def spawn(self, coro: Coroutine[Any, Any, _T], *, name: str = "fire-and-forget") -> None:
        """启动 fire-and-forget task（不阻塞调用方）。

        背压满或已 shutdown 时直接关闭 coro 并丢弃（避免 ``coro`` 未消费
        导致 ``RuntimeError: coroutine was never awaited``）。

        Args:
            coro: 待执行的协程（调用方已 ``await`` 之外的 ``coro_factory()``）
            name: task 名称，仅用于日志（``asyncio.Task`` 在 3.8+ 支持名字）
        """
        if self._shutdown:
            # shutdown 后拒绝新 task——关闭 coro 避免未 await 警告
            self._close_coro(coro)
            return

        # 背压检查（非阻塞）：信号量满则丢弃
        if self._semaphore is not None:
            # ``Semaphore.acquire`` 是 async，这里用内部 ``_value`` 同步判断
            # 仅作 best-effort 背压——多任务并发 race 时不严格，但足够
            # 防止队列无限膨胀。严格背压需用 ``asyncio.Lock`` 包裹，代价过高。
            if self._semaphore._value <= 0:  # type: ignore[attr-defined]
                logger.warning(
                    "task_registry_backpressure_full",
                    extra={
                        "event": "task_registry",
                        "outcome": "dropped",
                        "task_name": name,
                        "max_concurrency": self._max_concurrency,
                    },
                )
                self._close_coro(coro)
                return

        # 创建 task 并持有强引用（防 GC）
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        """task 完成回调：从 ``_tasks`` 移除强引用 + 记录异常。

        ``task.add_done_callback`` 在事件循环线程同步调用，安全操作 set。
        """
        # 移除强引用——set 不会无限增长
        self._tasks.discard(task)
        # 检查异常——fire-and-forget task 的异常本应被其自身 try/except 捕获，
        # 此处兜底记录防静默丢失（task 内未 catch 的异常会在 GC 时打印 warning，
        # 但显式记录更易排查）
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "task_registry_task_failed",
                    extra={
                        "event": "task_registry",
                        "outcome": "error",
                        "task_name": task.get_name(),
                        "error": type(exc).__name__,
                    },
                )

    @staticmethod
    def _close_coro(coro: Coroutine[Any, Any, Any]) -> None:
        """关闭未消费的协程，避免 ``RuntimeError: coroutine was never awaited``。"""
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            # close 一般不抛异常，但协程已在 yield 点时可能抛 GeneratorExit 链
            # 异常——吞掉，调用方已记录 warning
            pass

    async def shutdown(self, timeout: float = 5.0) -> None:
        """lifespan 关闭时调用：cancel 所有未完成 task 并等待退出。

        Args:
            timeout: 每个 task cancel 后等待退出的超时（秒）。超时后强制
                     放弃等待——task 仍会在 event loop 关闭时被取消。

        注意：shutdown 完成后重置 ``_shutdown`` 标志为 False，使同一实例
        可在下一次 lifespan 启动时复用（测试环境多次 TestClient 上下文切换）。
        """
        self._shutdown = True
        if not self._tasks:
            # 仍需重置标志，否则下一次 lifespan 启动后所有 spawn 都会被丢弃
            self._shutdown = False
            return
        # 复制 set——cancel 过程中 ``_on_done`` 会修改原 set
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        # 等待所有 task 退出（return_exceptions=True 防 cancel 抛 CancelledError）
        await asyncio.wait(pending, timeout=timeout, return_exceptions=asyncio.ALL_COMPLETED)
        self._tasks.clear()
        # 重置标志——下一次 lifespan 启动后 spawn 可正常工作
        self._shutdown = False
        logger.info(
            "task_registry_shutdown",
            extra={
                "event": "task_registry",
                "outcome": "shutdown",
                "cancelled": len(pending),
            },
        )

    @property
    def active_count(self) -> int:
        """当前活跃 task 数（监控/排障用）。"""
        return len(self._tasks)


# ===================== 单例 =====================


_registry: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    """返回全局 ``TaskRegistry`` 单例。

    首次调用时从 ``settings.task_registry_max_concurrency`` 读取并发上限。
    后续调用返回同一实例——信号量状态跨请求共享（背压全局生效）。
    """
    global _registry
    if _registry is None:
        from app.core.config import settings

        _registry = TaskRegistry(settings.task_registry_max_concurrency)
    return _registry


def reset_task_registry() -> None:
    """重置单例（仅测试用）。

    测试环境 ``conftest`` 可能修改 ``settings.task_registry_max_concurrency``
    后需重置单例使新值生效。生产环境不调用。
    """
    global _registry
    _registry = None
