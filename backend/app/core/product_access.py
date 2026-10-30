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

import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.db.models import Product
from app.utils.logger import logger


# ---------------------------------------------------------------------------
# v4.1.9：产品可见性矩阵的进程内缓存
#
# 背景：get_visible_product_ids 每次调用都 `select(Product.id,
# visible_to_user_ids)` 全表扫。v4.1.9 修复产品隔离后，它被挂到 8 个读路径上
# （knowledge 3 / review_reports 2 / dedup 1 / products 依赖图谱 / 守卫），
# 单页并发 5-10 个请求就是 5-10 次全表扫描。产品表现在只有几十行（每次 1-3ms），
# 但产品数上千后就是热点。
#
# 缓存的是「产品 → 可见名单」**原始矩阵**，不按用户缓存：
# 过滤（user_id in vids）在每次调用时做，所以不同用户共享同一份缓存。
#
# 失效：
#   - 写路径（products.py 的 create/update/delete）主动 invalidate_product_cache()
#   - TTL 兜底（5s），覆盖漏调失效或直接改 DB 的情况
#   - 进程内缓存：多实例部署时各进程独立。本项目 offline 单实例，可接受；
#     将来多实例需换成 Redis 或去掉缓存。
#
# 为什么 assert_product_visible 不一起缓存：它走 session.get(Product, pid)
# 主键查询（1ms 级），且对「产品不存在 → 放行」的语义有依赖；改成读矩阵会把
# 一个便宜且语义清晰的操作变复杂，收益不成比例。
# ---------------------------------------------------------------------------

_PRODUCT_VIS_TTL_SECONDS = 5.0
_vis_matrix_cache: tuple[float, list[tuple[str, list[int]]]] | None = None


def invalidate_product_cache() -> None:
    """清空产品可见性缓存。

    产品创建 / 改名 / 修改可见名单 / 删除后必须调用，否则最长 5s 内
    仍按旧名单判定（新增产品会因「不在矩阵里」被当作不存在而放行）。
    """
    global _vis_matrix_cache
    _vis_matrix_cache = None


async def _load_visibility_matrix(session: AsyncSession) -> list[tuple[str, list[int]]]:
    """返回 [(product_id, visible_to_user_ids)]，带 5s 进程内缓存。"""
    global _vis_matrix_cache
    now = time.monotonic()
    cached = _vis_matrix_cache
    if cached is not None and now - cached[0] < _PRODUCT_VIS_TTL_SECONDS:
        return cached[1]

    rows = await session.execute(select(Product.id, Product.visible_to_user_ids))
    matrix = [(pid, list(vids or [])) for pid, vids in rows.all()]
    _vis_matrix_cache = (now, matrix)
    return matrix


async def get_visible_product_ids(session: AsyncSession, user: CurrentUser) -> list[str] | None:
    """返回当前用户可见的产品 ID 列表。

    Returns:
        list[str]: 可见产品 ID 集合
        None: 无限制（admin，或可见性过滤整体跳过）
    """
    if user.is_admin:
        return None

    matrix = await _load_visibility_matrix(session)
    return [pid for pid, vids in matrix if not vids or user.user_id in vids]


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
