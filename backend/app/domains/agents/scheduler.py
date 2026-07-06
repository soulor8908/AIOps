"""Agent autonomous loop — 后台 worker（P0-2）。

周期扫描 ``schedule_enabled=True`` 且 ``next_run_at <= now`` 的 Agent，并发执行。
每个 agent 独立 session + 超时 + 错误隔离，单 agent 失败不阻塞其他 agent。

设计要点：
- **单 worker 假设**：P0-2 阶段单进程单 worker，``mark_agent_run_started`` 的
  flush+commit 即可防止本轮重复选中。多 worker 部署需引入 lease/SKIP LOCKED，
  留待 P2-9 A2A 异步消息阶段升级。
- **错误隔离**：``_execute_one`` 捕获所有异常（含 timeout），标记 ``failed``/
  ``timeout`` 后正常返回，``gather(return_exceptions=True)`` 二次兜底。
- **超时**：``asyncio.wait_for`` 包裹 ``execute_agent``，超时标记 ``timeout``。
  注意超时后 ``execute_agent`` 内部的 LLM 调用仍可能在线程池中跑完（无法强制
  取消 httpx 请求），但结果被丢弃，且 session 在 ``_execute_one`` 的
  ``async with`` 退出时关闭。
- **trigger input**：scheduled 触发无用户输入，用固定 ``_SCHEDULED_TRIGGER_INPUT``
  保持 ``ExecuteRequest`` 契约不变。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.metrics import metrics
from app.domains.agents import service
from app.domains.agents.models import Agent, ExecuteRequest

logger = logging.getLogger("app.agents.scheduler")

# scheduled 触发的固定输入（service 层同名常量，此处避免跨模块私有引用）
_SCHEDULED_TRIGGER_INPUT = "scheduled autonomous run"


async def _execute_one(agent: Agent) -> None:
    """执行单个 scheduled agent：独立 session + 超时 + 错误隔离。

    无论成功/失败/超时都写 ``last_run_status`` 并记录 metrics，保证 worker
    下一轮能按 ``next_run_at`` 再次选中（失败不阻塞后续调度）。
    """
    agent_id = agent.id
    agent_name = agent.name
    timeout_secs = settings.agent_scheduler_timeout_seconds
    start = time.monotonic()
    status = "success"
    error: str | None = None
    try:
        async with AsyncSessionLocal() as session:
            await asyncio.wait_for(
                service.execute_agent(
                    session,
                    agent_id,
                    ExecuteRequest(input=_SCHEDULED_TRIGGER_INPUT),
                ),
                timeout=timeout_secs,
            )
    except TimeoutError:
        status = "timeout"
        error = f"execution exceeded {timeout_secs}s timeout"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = (time.monotonic() - start) * 1000

    # 用独立 session 写结束状态（execute_agent 的 session 已随 async with 关闭）
    try:
        async with AsyncSessionLocal() as session:
            fresh = await session.get(Agent, agent_id)
            if fresh is not None:
                await service.mark_agent_run_finished(
                    session,
                    fresh,
                    status=status,
                    now=datetime.now(UTC),
                    error=error,
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to mark agent %s finished (status=%s)", agent_name, status
        )

    metrics.record_agent_run(agent_name, status, duration_ms)
    logger.info(
        "scheduled agent run name=%s status=%s duration_ms=%.0f",
        agent_name,
        status,
        duration_ms,
    )


async def tick() -> int:
    """单次扫描：查到期 agent → 标记 started → 并发执行。

    返回本轮选中的 agent 数。单 agent 失败由 ``_execute_one`` 内部隔离，
    ``gather(return_exceptions=True)`` 二次兜底，tick 本身不抛异常。

    P0-17：tick 开始先调用 ``recover_stuck_agents`` 恢复上一轮 pod 崩溃 /
    SIGKILL 遗留的卡死 agent（``last_run_status="running"`` 超 lease），
    恢复后按正常流程查询 due agent。
    """
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        # P0-17：恢复卡死 agent（lease 过期）。失败不阻塞 tick——最坏情况是
        # 卡死 agent 多停一轮，下一轮 tick 再恢复。
        try:
            recovered = await service.recover_stuck_agents(
                session, now, settings.agent_scheduler_lease_seconds
            )
            if recovered:
                logger.warning("P0-17 recovered %d stuck agent(s)", recovered)
        except Exception:  # noqa: BLE001
            logger.exception("P0-17 recover_stuck_agents failed, skip")
        due = await service.list_due_agents(session, now)
        if not due:
            return 0
        for agent in due:
            await service.mark_agent_run_started(session, agent, now)
        await session.commit()  # 持久化 started 状态，防止本轮重复选中

    # session 外并发执行（agent 对象因 expire_on_commit=False 仍可读 id/name）
    sem = asyncio.Semaphore(settings.agent_scheduler_concurrency)

    async def _bounded(a: Agent) -> None:
        async with sem:
            await _execute_one(a)

    await asyncio.gather(*(_bounded(a) for a in due), return_exceptions=True)
    return len(due)


async def run_scheduler_loop() -> None:
    """autonomous loop 主循环：每 ``interval`` 秒 tick 一次，直到被 cancel。

    被 ``main.lifespan`` 在 shutdown 时 cancel，捕获 ``CancelledError`` 优雅退出。
    单 tick 异常被捕获并记录，不杀掉循环（worker 需长期稳定运行）。
    """
    logger.info(
        "agent scheduler started interval=%ds concurrency=%d timeout=%ds",
        settings.agent_scheduler_interval_seconds,
        settings.agent_scheduler_concurrency,
        settings.agent_scheduler_timeout_seconds,
    )
    try:
        while True:
            try:
                n = await tick()
                if n:
                    logger.info("agent scheduler tick executed %d agent(s)", n)
            except Exception:  # noqa: BLE001
                logger.exception("agent scheduler tick failed")
            await asyncio.sleep(settings.agent_scheduler_interval_seconds)
    except asyncio.CancelledError:
        logger.info("agent scheduler stopped")
        raise
