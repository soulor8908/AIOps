"""Prompt Studio — 单元测试。

覆盖 service 纯函数：create_prompt / create_version / rollback / diff。
使用 SQLite in-memory async session（无 PG 依赖）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.prompts import service
from app.domains.prompts.models import (
    PromptCreate,
    PromptUpdate,
    PromptVersionCreate,
)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """SQLite in-memory async session。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    # SQLite 不支持 VECTOR/JSONB，Prompt 模型仅用 JSONB —— 测试用方言替换
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_prompt_initial_version(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session,
        PromptCreate(name="greeting", description="打招呼", content="Hello {name}"),
    )
    assert prompt.name == "greeting"
    assert prompt.current_version_id is not None
    versions = await service.list_prompts(session)
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_list_prompts_search_escapes_wildcards(session: AsyncSession) -> None:
    """P2：搜索词中的 % / _ 被转义为字面量，避免通配符注入。"""
    await service.create_prompt(session, PromptCreate(name="a%b", content="c"))
    await service.create_prompt(session, PromptCreate(name="acd", content="c"))
    await service.create_prompt(session, PromptCreate(name="normal", content="c"))

    # 搜索字面 "%" 应只匹配 "a%b"，而非匹配全部
    result = await service.list_prompts(session, q="%")
    assert len(result) == 1
    assert result[0].name == "a%b"

    # 搜索字面 "_" 应只匹配 "a%b"（无单独 "_" 名），不应匹配 "acd"（_ 匹配任意单字符）
    result_underscore = await service.list_prompts(session, q="a_b")
    assert {p.name for p in result_underscore} == {"a%b"}


@pytest.mark.asyncio
async def test_create_version_increments(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session, PromptCreate(name="p1", content="v1 content")
    )
    v2 = await service.create_version(
        session, prompt.id, PromptVersionCreate(content="v2 content", change_note="tweak")
    )
    assert v2.version_num == 2
    refreshed = await service.get_prompt(session, prompt.id)
    assert refreshed.current_version_id == v2.id


@pytest.mark.asyncio
async def test_rollback_creates_new_version(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session, PromptCreate(name="p2", content="original")
    )
    # 通过 selectinload 预加载 versions，避免 async 懒加载触发 MissingGreenlet
    loaded = await service.get_prompt(session, prompt.id)
    v1 = loaded.versions[0]
    await service.create_version(
        session, prompt.id, PromptVersionCreate(content="modified")
    )
    rolled = await service.rollback_prompt(session, prompt.id, v1.id)
    assert rolled.version_num == 3
    assert rolled.content == "original"
    assert "rollback" in (rolled.change_note or "")


@pytest.mark.asyncio
async def test_diff_versions_detects_changes(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session, PromptCreate(name="p3", content="line1\nline2")
    )
    await service.create_version(
        session, prompt.id, PromptVersionCreate(content="line1\nline2\nline3")
    )
    diff = await service.diff_versions(session, prompt.id, 1, 2)
    assert diff.from_version == 1
    assert diff.to_version == 2
    assert any("line3" in line for line in diff.added_lines)


@pytest.mark.asyncio
async def test_delete_prompt_cascades(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session, PromptCreate(name="to-delete", content="x")
    )
    pid = prompt.id
    await service.delete_prompt(session, pid)
    with pytest.raises(Exception):
        await service.get_prompt(session, pid)


@pytest.mark.asyncio
async def test_update_prompt_metadata(session: AsyncSession) -> None:
    prompt = await service.create_prompt(
        session, PromptCreate(name="upd", content="c")
    )
    updated = await service.update_prompt(
        session, prompt.id, PromptUpdate(description="new desc", is_active=False)
    )
    assert updated.description == "new desc"
    assert updated.is_active is False
