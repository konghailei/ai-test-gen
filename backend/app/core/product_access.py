"""产品数据可见性控制（v3.1.3）。

语义（与 Product.visible_to_user_ids 字段注释一致）：
    - visible_to_user_ids 为空列表 → 所有人可见（向后兼容：存量数据不受影响）
    - 非空列表 → 仅名单内用户 + admin 可见

接入点：
    - 列表接口：查询条件追加 Product.id IN (可见集合)
    - 单资源接口（详情/更新/删除/反馈/导出）：取到资源后 assert_product_visible

注意：所有 helper 对 admin 直接放行；product_id 为空（历史脏数据）时放行，
由业务层的 404 逻辑兜底，避免可见性校验把存量数据整体隐藏。
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.db.models import Product
from app.utils.logger import logger


async def get_visible_product_ids(session: AsyncSession, user: CurrentUser) -> list[str] | None:
    """返回当前用户可见的产品 ID 列表。

    Returns:
        list[str]: 可见产品 ID 集合
        None: 无限制（admin，或可见性过滤整体跳过）
    """
    if user.is_admin:
        return None
    rows = await session.execute(select(Product.id, Product.visible_to_user_ids))
    visible: list[str] = []
    for pid, vids in rows.all():
        if not (vids or []) or user.user_id in vids:
            visible.append(pid)
    return visible


async def assert_product_visible(session: AsyncSession, user: CurrentUser, product_id: str | None) -> None:
    """校验单个产品对当前用户可见，不可见抛 403。

    admin 放行；product_id 为空放行（历史数据兜底，交给业务层 404）。
    """
    if user.is_admin or not product_id:
        return
    row = await session.get(Product, product_id)
    if row is None:
        return  # 产品不存在：业务层负责 404，可见性层不重复报错
    vids = row.visible_to_user_ids or []
    if vids and user.user_id not in vids:
        logger.warning(
            "[ProductAccess] 用户 {}(id={}) 尝试访问未授权产品 {}",
            user.username, user.user_id, product_id,
        )
        raise HTTPException(status_code=403, detail="无权访问该产品数据")


def apply_visible_product_filter(conditions: list, visible_ids: list[str] | None, column) -> None:
    """把可见性过滤追加到查询条件列表。

    Args:
        conditions: 现有查询条件列表（就地追加）
        visible_ids: get_visible_product_ids 的返回值；None 表示不过滤
        column: 参与过滤的列（如 TestCase.product_id / Product.id / Requirement.product_id）
    """
    if visible_ids is None:
        return
    if not visible_ids:
        # 名单内一个产品都不可见 → 显式返回空集（不能省略条件，否则等于全量泄漏）
        conditions.append(column.in_(["__no_visible_products__"]))
        return
    conditions.append(column.in_(visible_ids))
