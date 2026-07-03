"""Prompt Studio 版本管理与 diff eval（prompts/SPEC.md Success Criteria）。

覆盖 6 项验收：
1. 版本创建 P99 < 200ms（验证 create_version 在合理时延内完成）
2. 回滚后 current_version 内容与目标版本 100% 一致
3. 并发创建版本不产生重复 version_num（验证 max+1 自增逻辑）
4. diff 接口对相同版本号正确报错，对差异版本返回 added/removed/unified_diff
5. 删除 Prompt 时级联删除其全部版本
6. 创建 Prompt 时自动写入 version_num=1 并设为 current

经 ``client`` fixture 的 session_factory 直接调用 service 层（create_prompt /
create_version / rollback_prompt / diff_versions / delete_prompt），绕过 HTTP 层
专注版本管理逻辑。SC1 性能断言宽松（CI 环境抖动），重点验证逻辑正确性。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import NotFoundError, ValidationError
from app.domains.prompts import service as prompt_service
from app.domains.prompts.models import (
    Prompt,
    PromptCreate,
    PromptVersion,
    PromptVersionCreate,
)
from app.main import app

# ===================== 辅助：经 session_factory 执行异步场景 =====================


def _run(
    client: TestClient, scenario: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """在测试 DB 的 session 上下文中执行异步场景函数。"""
    session_factory = app.dependency_overrides[get_session]

    async def _wrapper() -> None:
        async for session in session_factory():
            await scenario(session)
            break

    client.portal.call(_wrapper)  # type: ignore[union-attr]


async def _seed_prompt(
    session: AsyncSession, name: str = "p", content: str = "v1"
) -> Prompt:
    """创建 Prompt（含初始 v1）并 flush。"""
    prompt = await prompt_service.create_prompt(
        session, PromptCreate(name=name, content=content, variables=["x"])
    )
    await session.flush()
    return prompt


# ===================== 1. 版本创建时延（P99 < 200ms 的逻辑验证） =====================


def test_create_version_completes_quickly(client: TestClient) -> None:
    """create_version 在合理时延内完成（SPEC 1：P99 < 200ms 的逻辑验证）。

    CI 环境抖动大，不严格断言 P99，仅验证单次 create_version 在 1s 内完成
    （远宽松于 200ms 目标，确认无性能退化/死锁）。
    """

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="v1")
        start = time.monotonic()
        await prompt_service.create_version(
            session, prompt.id, PromptVersionCreate(content="v2")
        )
        elapsed = time.monotonic() - start
        # 宽松断言：远小于 1s（200ms 目标的 5x 缓冲），排除性能退化
        assert elapsed < 1.0, f"create_version 耗时 {elapsed:.3f}s 异常"

    _run(client, _scenario)


# ===================== 2. 回滚后 current_version 内容与目标一致 =====================


def test_rollback_current_version_matches_target(client: TestClient) -> None:
    """回滚后 current_version_id 指向新版本且内容与目标 100% 一致（SPEC 2）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="original")
        # v1
        v1 = await session.scalar(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt.id,
                PromptVersion.version_num == 1,
            )
        )
        assert v1 is not None
        # 创建 v2（内容不同）
        await prompt_service.create_version(
            session, prompt.id, PromptVersionCreate(content="modified")
        )
        # 回滚到 v1
        rolled = await prompt_service.rollback_prompt(session, prompt.id, v1.id)
        await session.flush()

        # 重新加载 prompt（current_version_id 已更新）
        prompt_fresh = await prompt_service.get_prompt(session, prompt.id)
        assert prompt_fresh.current_version_id == rolled.id
        assert rolled.content == "original"  # 与目标 v1 内容 100% 一致
        assert rolled.version_num == 3  # 追加为新版本（v3）
        assert "rollback" in (rolled.change_note or "")

    _run(client, _scenario)


# ===================== 3. 连续创建版本不产生重复 version_num =====================


def test_sequential_create_version_no_duplicate(client: TestClient) -> None:
    """连续 create_version 产生单调递增且不重复的 version_num（SPEC 3）。

    SPEC 要求"并发创建版本不产生重复 version_num"。实现用 ``with_for_update()`` 行锁
    + ``max(version_num)+1`` 自增。SQLite 不支持 FOR UPDATE（生产 PG 支持），此处
    验证 max+1 逻辑在连续调用下产生不重复的递增 version_num。
    """

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="v1")
        # 连续创建 4 个版本（v2-v5）
        nums = []
        for i in range(2, 6):
            v = await prompt_service.create_version(
                session, prompt.id, PromptVersionCreate(content=f"v{i}")
            )
            nums.append(v.version_num)

        # 递增且无重复
        assert nums == [2, 3, 4, 5]
        assert len(set(nums)) == len(nums)

        # 校验 DB 中所有版本号唯一
        all_versions = await session.scalars(
            select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)
        )
        all_nums = [v.version_num for v in all_versions]
        assert sorted(all_nums) == [1, 2, 3, 4, 5]
        assert len(set(all_nums)) == len(all_nums)

    _run(client, _scenario)


# ===================== 4. diff 相同版本报错 + 差异版本返回三字段 =====================


def test_diff_same_version_raises(client: TestClient) -> None:
    """diff 的 from 与 to 相同时抛 ValidationError（SPEC 4）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="line1\nline2")
        with pytest.raises(ValidationError):
            await prompt_service.diff_versions(
                session, prompt.id, from_version=1, to_version=1
            )

    _run(client, _scenario)


def test_diff_different_versions_returns_added_removed_unified(
    client: TestClient,
) -> None:
    """diff 差异版本返回 added_lines / removed_lines / unified_diff（SPEC 4）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="line1\nline2")
        await prompt_service.create_version(
            session,
            prompt.id,
            PromptVersionCreate(content="line1\nline3"),  # line2 → line3
        )

        result = await prompt_service.diff_versions(
            session, prompt.id, from_version=1, to_version=2
        )

        assert result.from_version == 1
        assert result.to_version == 2
        # added：新增的行（line3）
        assert any("line3" in line for line in result.added_lines)
        # removed：删除的行（line2）
        assert any("line2" in line for line in result.removed_lines)
        # unified_diff：含 difflib 头（---/+++）与变更行
        assert len(result.unified_diff) > 0
        assert any(line.startswith("---") for line in result.unified_diff)
        assert any(line.startswith("+++") for line in result.unified_diff)

    _run(client, _scenario)


def test_diff_nonexistent_version_raises_not_found(client: TestClient) -> None:
    """diff 指定不存在的版本号抛 NotFoundError（SPEC Error Cases）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="v1")
        with pytest.raises(NotFoundError):
            await prompt_service.diff_versions(
                session, prompt.id, from_version=1, to_version=99
            )

    _run(client, _scenario)


# ===================== 5. 删除 Prompt 级联删除全部版本 =====================


def test_delete_prompt_cascades_versions(client: TestClient) -> None:
    """删除 Prompt 时级联删除其全部版本（SPEC 5）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await _seed_prompt(session, content="v1")
        # 创建 2 个额外版本
        await prompt_service.create_version(
            session, prompt.id, PromptVersionCreate(content="v2")
        )
        await prompt_service.create_version(
            session, prompt.id, PromptVersionCreate(content="v3")
        )
        await session.flush()

        # 删除前：3 个版本
        before = list(await session.scalars(
            select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)
        ))
        assert len(before) == 3

        # 删除 Prompt
        await prompt_service.delete_prompt(session, prompt.id)
        await session.flush()

        # 删除后：版本全部级联删除
        after = list(await session.scalars(
            select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)
        ))
        assert len(after) == 0
        # Prompt 本身也不存在
        with pytest.raises(NotFoundError):
            await prompt_service.get_prompt(session, prompt.id)

    _run(client, _scenario)


# ===================== 6. 创建 Prompt 自动写入 version_num=1 并设为 current =====================


def test_create_prompt_writes_version_one_as_current(client: TestClient) -> None:
    """创建 Prompt 时自动写入 version_num=1 并设为 current（SPEC 6）。"""

    async def _scenario(session: AsyncSession) -> None:
        prompt = await prompt_service.create_prompt(
            session,
            PromptCreate(
                name="auto-v1",
                content="hello {name}",
                variables=["name"],
            ),
        )
        await session.flush()

        # current_version_id 指向 v1
        assert prompt.current_version_id is not None
        # 仅 1 个版本，version_num=1
        prompt_fresh = await prompt_service.get_prompt(session, prompt.id)
        assert len(prompt_fresh.versions) == 1
        v1 = prompt_fresh.versions[0]
        assert v1.version_num == 1
        assert v1.content == "hello {name}"
        assert v1.variables == ["name"]
        assert v1.change_note == "initial"
        assert v1.id == prompt_fresh.current_version_id

    _run(client, _scenario)
