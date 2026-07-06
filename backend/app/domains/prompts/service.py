"""Prompt Studio — 业务逻辑纯函数。

约束：
- 单 Prompt 最大 100 版本
- content ≤ 64KB（schema 层校验）
- 回滚即新增版本，内容为目标版本（保持版本链单调递增）
"""

from __future__ import annotations

import difflib
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.domains.prompts.models import (
    DiffResult,
    Prompt,
    PromptCreate,
    PromptUpdate,
    PromptVersion,
    PromptVersionCreate,
)

if TYPE_CHECKING:
    from app.domains.prompts.models import PromptOut, PromptVersionOut

MAX_VERSIONS = 100


async def create_prompt(
    session: AsyncSession, payload: PromptCreate, owner_id: uuid.UUID | None = None
) -> Prompt:
    """创建 Prompt 并写入初始版本（version_num=1）。

    P4-2：``owner_id`` 绑定创建者（router 层传 current_user.id）。
    """
    prompt = Prompt(name=payload.name, description=payload.description, owner_id=owner_id)
    session.add(prompt)
    await session.flush()
    v1 = PromptVersion(
        prompt_id=prompt.id,
        version_num=1,
        content=payload.content,
        variables=payload.variables,
        change_note="initial",
    )
    session.add(v1)
    await session.flush()
    prompt.current_version_id = v1.id
    await session.flush()
    return prompt


async def get_prompt(
    session: AsyncSession,
    prompt_id: uuid.UUID,
    owner_id: uuid.UUID | None = None,
) -> Prompt:
    """获取 Prompt（含 versions 关系）。不存在抛 NotFoundError。

    P4-2：``owner_id`` 非 None 时校验所有权——不匹配抛 NotFoundError(404,
    不泄露资源存在性)。``owner_id=None`` 表示 admin / 系统调用跳过校验。
    """
    stmt = (
        select(Prompt)
        .options(selectinload(Prompt.versions))
        .where(Prompt.id == prompt_id)
    )
    prompt = (await session.execute(stmt)).scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(f"Prompt {prompt_id} 不存在")
    # P4-2：非 admin 校验所有权。owner_id 为 NULL 的旧 Prompt 仅 admin 可见。
    if owner_id is not None and prompt.owner_id != owner_id:
        raise NotFoundError(f"Prompt {prompt_id} 不存在")
    return prompt


async def list_prompts(
    session: AsyncSession,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    owner_id: uuid.UUID | None = None,
) -> list[Prompt]:
    """列出 Prompt，支持名称模糊搜索。

    搜索词中的 ``%`` / ``_`` / ``\\`` 被转义为字面量，避免通配符注入
    （如搜索 ``%`` 匹配全部、``_`` 匹配任意单字符）破坏搜索语义。

    P4-2：``owner_id`` 非 None 时仅返回该用户的 Prompt。
    """
    stmt = select(Prompt).options(selectinload(Prompt.versions))
    if owner_id is not None:
        stmt = stmt.where(Prompt.owner_id == owner_id)
    if q:
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Prompt.name.ilike(f"%{q_escaped}%", escape="\\"))
    stmt = stmt.order_by(Prompt.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def update_prompt(
    session: AsyncSession, prompt_id: uuid.UUID, payload: PromptUpdate
) -> Prompt:
    """更新 Prompt 元信息。"""
    prompt = await get_prompt(session, prompt_id)
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(prompt, key, value)
    await session.flush()
    return prompt


async def delete_prompt(session: AsyncSession, prompt_id: uuid.UUID) -> None:
    """硬删除 Prompt（连带 versions 级联删除）。"""
    prompt = await get_prompt(session, prompt_id)
    await session.delete(prompt)
    await session.flush()


async def create_version(
    session: AsyncSession, prompt_id: uuid.UUID, payload: PromptVersionCreate
) -> PromptVersion:
    """新增版本。version_num 自增。超 100 抛 ConflictError。"""
    lock_stmt = (
        select(Prompt)
        .where(Prompt.id == prompt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    prompt = (await session.execute(lock_stmt)).scalar_one_or_none()
    if prompt is None:
        raise NotFoundError(f"Prompt {prompt_id} 不存在")
    # P3：合并 count + max 为单条查询，缩短行级锁持有时间（原 2 次 DB 往返 → 1 次）。
    stats_stmt = select(
        func.count(),
        func.coalesce(func.max(PromptVersion.version_num), 0),
    ).where(PromptVersion.prompt_id == prompt_id)
    count, max_num = (await session.execute(stats_stmt)).one()
    count = int(count)
    if count >= MAX_VERSIONS:
        raise ConflictError(f"Prompt 版本数已达上限 {MAX_VERSIONS}")
    next_num = int(max_num) + 1
    version = PromptVersion(
        prompt_id=prompt_id,
        version_num=next_num,
        content=payload.content,
        variables=payload.variables,
        change_note=payload.change_note,
    )
    session.add(version)
    await session.flush()
    prompt.current_version_id = version.id
    await session.flush()
    return version


async def rollback_prompt(
    session: AsyncSession, prompt_id: uuid.UUID, version_id: uuid.UUID
) -> PromptVersion:
    """回滚：以目标版本内容创建新版本（追加式回滚，保留历史）。"""
    await get_prompt(session, prompt_id)  # 校验 prompt 存在
    target_stmt = select(PromptVersion).where(
        PromptVersion.id == version_id, PromptVersion.prompt_id == prompt_id
    )
    target = (await session.execute(target_stmt)).scalar_one_or_none()
    if target is None:
        raise NotFoundError(f"版本 {version_id} 不存在")
    return await create_version(
        session,
        prompt_id,
        PromptVersionCreate(
            content=target.content,
            variables=[str(v) for v in target.variables],
            change_note=f"rollback to v{target.version_num}",
        ),
    )


async def diff_versions(
    session: AsyncSession,
    prompt_id: uuid.UUID,
    from_version: int,
    to_version: int,
    owner_id: uuid.UUID | None = None,
) -> DiffResult:
    """对两个版本号做行级 diff。

    P4-2：``owner_id`` 非 None 时校验所有权（透传给 get_prompt）。
    """
    if from_version == to_version:
        raise ValidationError("from 与 to 不能相同")
    prompt = await get_prompt(session, prompt_id, owner_id=owner_id)
    from_content = await _fetch_version_content(session, prompt.id, from_version)
    to_content = await _fetch_version_content(session, prompt.id, to_version)
    from_lines = from_content.splitlines(keepends=True)
    to_lines = to_content.splitlines(keepends=True)
    added: list[str] = []
    removed: list[str] = []
    unified = list(difflib.unified_diff(from_lines, to_lines, n=2))
    for line in unified:
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].rstrip("\n"))
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:].rstrip("\n"))
    return DiffResult(
        from_version=from_version,
        to_version=to_version,
        added_lines=added,
        removed_lines=removed,
        unified_diff=[line.rstrip("\n") for line in unified],
    )


async def _fetch_version_content(
    session: AsyncSession, prompt_id: uuid.UUID, version_num: int
) -> str:
    stmt = select(PromptVersion).where(
        PromptVersion.prompt_id == prompt_id,
        PromptVersion.version_num == version_num,
    )
    version = (await session.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise NotFoundError(f"版本 v{version_num} 不存在")
    return version.content


def to_prompt_out(prompt: Prompt) -> PromptOut:
    """ORM → DTO。"""
    from app.domains.prompts.models import PromptOut
    return PromptOut.from_orm_with_versions(prompt)


def to_version_out(version: PromptVersion) -> PromptVersionOut:
    """ORM → DTO。"""
    from app.domains.prompts.models import PromptVersionOut
    return PromptVersionOut.model_validate(version)


# 重新导出供 router / api 使用
__all__ = [
    "MAX_VERSIONS",
    "create_prompt",
    "create_version",
    "delete_prompt",
    "diff_versions",
    "get_prompt",
    "list_prompts",
    "rollback_prompt",
    "to_prompt_out",
    "to_version_out",
    "update_prompt",
]
