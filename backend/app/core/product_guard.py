"""产品级资源归属解析（v4.1.9）。

背景
----
v3.1.3 P1-1 在查询侧加了产品可见性过滤，requirements.py 通过
``APIRouter(dependencies=[Depends(_guard_product_visibility)])`` 覆盖了全部
``/{req_id}/...`` 端点。但 products / review_reports / knowledge / dedup_removals
的若干写端点与列表端点仍以「有写权限即可」放行——只要知道资源 ID
（``PROJ-xxx`` / ``MOD-xxx`` / ``RR-xxx``，均为可枚举的「前缀-时间戳-序号」格式），
任何具备 write 权限的普通用户都能跨产品操作。

职责边界
--------
- ``core/product_access.py``：只判定「这个 product_id 对当前用户是否可见」
- ``core/product_guard.py``：只解析「这个 proj_id / mod_id / fp_id 属于哪个产品」

解析后交给 ``assert_product_visible``，二者不重复实现判定逻辑。

统一语义（与 product_access 一致）
----------------------------------
- 资源不存在 → 返回 None，调用方放行，由端点自身返回 404（不重复报错）
- 解析出空 product_id（历史脏数据）→ 放行，由业务层兜底
- admin → ``assert_product_visible`` 内部直接放行

为什么用 select(column) 而不是 session.get
------------------------------------------
路由级守卫会在每个请求上执行，只取一列可以避免把整行 ORM 对象读进 identity map
（products.py 的守卫一次可能要解析两个资源）。端点内部若已 ``session.get`` 过同名
对象，SQLAlchemy 会命中 identity map，不会产生第二次 SQL。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AnalysisTask,
    DedupRemoval,
    FPDependencyVersion,
    FunctionalPoint,
    Module,
    Project,
    Requirement,
    ReviewReportRecord,
    TestCase,
)


async def product_id_of_requirement(session: AsyncSession, req_id: str | None) -> str | None:
    """需求 → 产品。"""
    if not req_id:
        return None
    return (
        await session.execute(select(Requirement.product_id).where(Requirement.id == req_id))
    ).scalar_one_or_none()


async def product_id_of_task(session: AsyncSession, task_id: str | None) -> str | None:
    """分析任务 → 关联需求 → 产品。"""
    if not task_id:
        return None
    req_id = (
        await session.execute(
            select(AnalysisTask.req_id).where(AnalysisTask.id == task_id)
        )
    ).scalar_one_or_none()
    return await product_id_of_requirement(session, req_id)


async def product_id_of_project(session: AsyncSession, proj_id: str | None) -> str | None:
    """项目 → 产品。"""
    if not proj_id:
        return None
    return (
        await session.execute(select(Project.product_id).where(Project.id == proj_id))
    ).scalar_one_or_none()


async def product_id_of_module(session: AsyncSession, mod_id: str | None) -> str | None:
    """模块 → 产品。"""
    if not mod_id:
        return None
    return (
        await session.execute(select(Module.product_id).where(Module.id == mod_id))
    ).scalar_one_or_none()


async def product_id_of_fp(session: AsyncSession, fp_id: str | None) -> str | None:
    """功能点 → 需求 → 产品。"""
    if not fp_id:
        return None
    req_id = (
        await session.execute(
            select(FunctionalPoint.requirement_id).where(FunctionalPoint.id == fp_id)
        )
    ).scalar_one_or_none()
    return await product_id_of_requirement(session, req_id)


async def product_id_of_dependency_version(
    session: AsyncSession, version_pk: int | None
) -> str | None:
    """依赖版本快照 → 项目 → 产品。

    ``fp_dependency_versions`` 只有 project_id，没有冗余 product_id 列。
    """
    if version_pk is None:
        return None
    proj_id = (
        await session.execute(
            select(FPDependencyVersion.project_id).where(
                FPDependencyVersion.id == version_pk
            )
        )
    ).scalar_one_or_none()
    return await product_id_of_project(session, proj_id)


async def product_id_of_report(session: AsyncSession, report_id: str | None) -> str | None:
    """评审报告 → 产品（表上已有 product_id 列）。"""
    if not report_id:
        return None
    return (
        await session.execute(
            select(ReviewReportRecord.product_id).where(
                ReviewReportRecord.id == report_id
            )
        )
    ).scalar_one_or_none()


async def product_id_of_dedup_removal(
    session: AsyncSession, removal_id: str | None
) -> str | None:
    """去重留痕 → 需求 → 产品（表上无 product_id，只能回溯源需求）。"""
    if not removal_id:
        return None
    req_id = (
        await session.execute(
            select(DedupRemoval.req_id).where(DedupRemoval.removal_id == removal_id)
        )
    ).scalar_one_or_none()
    return await product_id_of_requirement(session, req_id)


async def product_id_of_test_case(session: AsyncSession, case_id: str | None) -> str | None:
    """用例 → 产品（用例表自带 product_id）。"""
    if not case_id:
        return None
    return (
        await session.execute(select(TestCase.product_id).where(TestCase.id == case_id))
    ).scalar_one_or_none()
