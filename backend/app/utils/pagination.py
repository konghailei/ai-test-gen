# -*- coding: utf-8 -*-
"""分页工具（v3.2.2 收敛重复）。

原先 count→order_by→execute 三段式样板在 7+ 个列表端点逐字复制，
total 计算、空值兜底、page/limit 解析逻辑散落。统一到此。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def paginate(
    session: AsyncSession,
    stmt,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list, int]:
    """执行带分页的查询，返回 (items, total)。

    自动附带 total 计数（COUNT 包裹原 stmt 的子查询），
    并做 page/page_size 边界保护。调用方传入已 order_by 的 stmt 即可。

    Args:
        session: 异步 DB 会话
        stmt: 已 order_by 的 select 语句（不含 limit/offset）
        page: 页码（1 起）
        page_size: 每页条数（上限 100，系统约定）

    Returns:
        (items, total)
    """
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    result = await session.execute(
        stmt.limit(page_size).offset((page - 1) * page_size)
    )
    return list(result.scalars().all()), int(total)
