"""知识库统计与模式管理路由。

端点清单（实现计划 T5.2）：
    GET /api/knowledge/stats     知识库统计（需求数/用例数/模式数/批次数/冷启动状态）
    GET /api/knowledge/patterns   测试模式列表

设计依据：
- docs/02-knowledge-base.md §3（测试模式库）
- docs/00-architecture-overview.md §9.1（冷启动模式判断）
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_admin
from app.db.database import get_session
from app.db.models import GenerationBatch, Requirement, TestCase, TestPattern
from app.db.vector_store import vector_store
from app.models.api_schemas import ApiResponse, KnowledgeStats
from app.utils.logger import logger


router = APIRouter()


# ============================================================================
# 1. 知识库统计
# ============================================================================

@router.get("/stats", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_knowledge_stats(
    product_id: str | None = Query(default=None, description="按产品筛选"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识库整体统计信息（支持按产品筛选）。"""
    # 构建产品过滤条件
    def _filter(stmt, model):
        return stmt.where(model.product_id == product_id) if product_id else stmt

    total_reqs = (await session.execute(_filter(select(func.count(Requirement.id)), Requirement))).scalar() or 0
    total_cases = (await session.execute(_filter(select(func.count(TestCase.id)), TestCase))).scalar() or 0
    total_patterns = (await session.execute(select(func.count(TestPattern.id)))).scalar() or 0
    # GenerationBatch 没有 product_id，按 req_id 关联的需求过滤
    if product_id:
        from sqlalchemy import and_
        total_batches = (await session.execute(
            select(func.count(GenerationBatch.id))
            .join(Requirement, GenerationBatch.req_id == Requirement.id)
            .where(Requirement.product_id == product_id)
        )).scalar() or 0
    else:
        total_batches = (await session.execute(select(func.count(GenerationBatch.id)))).scalar() or 0

    # 向量库是否为空（冷启动判断）
    kb_empty = False
    try:
        kb_empty = await asyncio.to_thread(vector_store.is_empty)
    except Exception:
        # 向量库未就绪时默认非空（不影响统计展示）
        pass

    stats = KnowledgeStats(
        total_requirements=total_reqs,
        total_test_cases=total_cases,
        total_patterns=total_patterns,
        total_batches=total_batches,
        kb_empty=kb_empty,
    )

    logger.info(
        "[API/knowledge] 统计: reqs={}, cases={}, patterns={}, batches={}, kb_empty={}",
        total_reqs, total_cases, total_patterns, total_batches, kb_empty,
    )

    return ApiResponse(
        success=True,
        message="ok",
        data=stats.model_dump(),
    )


# ============================================================================
# 2. 测试模式列表
# ============================================================================

@router.get("/patterns", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_patterns(
    category: str | None = Query(default=None, description="按类别筛选 crud/auth/state_machine/..."),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取测试模式列表（分页）。

    测试模式是 GenAgent 生成用例时参考的骨架，来源于种子数据和历史沉淀。

    Args:
        category: 按类别筛选（可选）
        page: 页码
        page_size: 每页条数
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items / total / page / page_size
    """
    # 计算总数
    count_stmt = select(func.count(TestPattern.id))
    if category:
        count_stmt = count_stmt.where(TestPattern.category == category)
    total = (await session.execute(count_stmt)).scalar() or 0

    # 分页查询
    stmt = select(TestPattern).order_by(TestPattern.usage_count.desc())
    if category:
        stmt = stmt.where(TestPattern.category == category)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        {
            "id": r.id,
            "name": r.name,
            "category": r.category,
            "pattern_json": r.pattern_json,
            "usage_count": r.usage_count,
            "success_rate": r.success_rate,
        }
        for r in rows
    ]

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


# ============================================================================
# 3. 知识库条目列表（需求 + 用例）
# ============================================================================

@router.get("/entries", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_kb_entries(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    type_filter: str | None = Query(default=None, alias="type", description="按类型筛选 requirement / test_case"),
    limit: int = Query(default=100, ge=1, le=500, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识库中的条目列表。

    从 SQLite 中读取已归档的需求和从向量库读取测试用例。
    支持按产品 ID 和类型筛选。

    Args:
        product_id: 按产品 ID 筛选（可选）
        type_filter: 按类型筛选 requirement / test_case（可选）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items（条目列表）/ total
    """
    items: list[dict] = []

    # 从 SQLite 读取已归档的需求（status='archived'）
    if not type_filter or type_filter == "requirement":
        stmt = select(Requirement).where(Requirement.status == "archived").order_by(Requirement.updated_at.desc())
        if product_id:
            stmt = stmt.where(Requirement.product_id == product_id)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        reqs = result.scalars().all()
        for r in reqs:
            summary = ""
            srr = r.srr_json or {}
            req_data = srr.get("requirement", {})
            title = req_data.get("title", r.title)
            summary = req_data.get("summary", "")[:300]
            items.append({
                "id": r.id,
                "title": title,
                "type": "requirement",
                "product_id": r.product_id,
                "modules": ",".join(r.modules or []),
                "priority": r.priority,
                "summary": summary,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            })

    # 从 SQLite 读取测试用例
    if not type_filter or type_filter == "test_case":
        stmt = select(TestCase).order_by(TestCase.created_at.desc()).limit(limit).offset(offset)
        if product_id:
            # 通过需求关联过滤（测试用例表有 req_id 字段）
            pass  # 暂不过滤产品
        result = await session.execute(stmt)
        cases = result.scalars().all()
        for c in cases:
            items.append({
                "id": c.id,
                "title": c.title,
                "type": "test_case",
                "product_id": "",
                "summary": f"类型: {c.type} / 优先级: {c.priority}" if c.type else "",
                "created_at": c.created_at.isoformat() if c.created_at else "",
            })

    # 按创建时间倒序排列
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "items": items,
            "total": len(items),
        },
    )


# ============================================================================
# 4. 删除知识库条目
# ============================================================================

@router.delete("/entries/{entry_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_kb_entry(
    entry_id: str,
    entry_type: str = Query(..., description="条目类型 requirement / test_case"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """从知识库中删除指定条目。

    删除策略：
    - requirement: 恢复为 active 状态（退出知识库）+ 从向量库移除
    - test_case: 从 SQLite 删除 + 从向量库移除
    """
    if entry_type == "requirement":
        # 恢复为 active 状态，退出知识库
        stmt = select(Requirement).where(Requirement.id == entry_id, Requirement.status == "archived")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail=f"需求 {entry_id} 不存在或未归档")
        record.status = "active"
        await session.commit()
        logger.info("[API/knowledge] 需求退出知识库: id={}", entry_id)

        # 从向量库移除
        try:
            await asyncio.to_thread(vector_store.delete_requirement_from_kb, entry_id)
        except Exception as e:
            logger.warning("[API/knowledge] 向量库删除失败(不阻断): {}", str(e)[:100])

    elif entry_type == "test_case":
        # 从 SQLite 删除测试用例
        stmt = select(TestCase).where(TestCase.id == entry_id)
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail=f"测试用例 {entry_id} 不存在")
        await session.delete(record)
        await session.commit()
        logger.info("[API/knowledge] 测试用例已删除: id={}", entry_id)

        # 从向量库移除
        try:
            await asyncio.to_thread(vector_store.delete_test_case_from_kb, entry_id)
        except Exception as e:
            logger.warning("[API/knowledge] 向量库删除失败(不阻断): {}", str(e)[:100])

    else:
        raise HTTPException(status_code=400, detail=f"不支持的条目类型: {entry_type}")

    return ApiResponse(
        success=True,
        message=f"已从知识库删除 {entry_type} {entry_id}",
        data={"entry_id": entry_id, "deleted": True},
    )
