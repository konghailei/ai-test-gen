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

from app.core.auth import get_current_user, require_admin, require_write
from app.db.database import get_session
from app.db.models import GenerationBatch, Project, Requirement, TestCase, TestPattern
from app.db.milvus_store import vector_store
from app.models.api_schemas import ApiResponse, KnowledgeStats
from app.utils.logger import logger


router = APIRouter()


# ============================================================================
# 辅助：构建需求向量化元数据 + 派发异步嵌入任务
# ============================================================================

def _build_requirement_kb_meta(record: Requirement, project_version: str) -> dict:
    """为已归档需求构建 Milvus 写入元数据（与归档接口保持一致）。"""
    ts = record.updated_at or record.created_at
    return {
        "product_id": record.product_id,
        "modules": ",".join(record.modules or []),
        "priority": record.priority,
        "status": "archived",
        "archived_at": ts.isoformat() if ts else "",
        "go_live_date": record.go_live_date.isoformat() if record.go_live_date else "",
        "project_version": project_version,
    }


def _dispatch_embedding_task(req_id: str, srr_json: dict, meta: dict) -> bool:
    """派发需求向量化异步任务；Celery 不可用时降级为同步写入。

    返回 True 表示任务已派发/已同步写入；False 表示派发与同步均失败
    （如模型未就绪）。注意：True 仅代表“已提交”，不保证 Milvus 已落库。
    """
    try:
        from app.tasks.celery_app import celery_app

        if celery_app is not None:
            celery_app.send_task(
                "app.tasks.embedding_tasks.embed_and_store_requirement",
                args=[req_id, srr_json, meta],
                queue="embedding",
            )
            return True
        # Celery 不可用 → 同步写入（会真正加载模型并写入 Milvus）
        vector_store.add_requirement(req_id, srr_json, metadata=meta)
        return True
    except Exception as e:
        logger.warning("[API/knowledge] 向量化任务派发失败: req_id={}, error={}", req_id, str(e)[:200])
        return False


async def _project_version(session: AsyncSession, project_id: str | None) -> str:
    if not project_id:
        return ""
    try:
        row = (await session.execute(select(Project.version).where(Project.id == project_id))).scalar_one_or_none()
        return row or ""
    except Exception:
        return ""


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
    except Exception as e:
        logger.warning("[API/knowledge] 向量库状态检查失败（默认非空）: {}", str(e)[:150])

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
    search: str | None = Query(default=None, description="按标题模糊搜索"),
    vectorized: str | None = Query(default=None, description="按向量化状态筛选: true / false"),
    limit: int = Query(default=100, ge=1, le=500, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识库中的条目列表。

    从 PostgreSQL 中读取已归档的需求和从向量库读取测试用例。
    支持按产品 ID、类型、标题搜索、向量化状态筛选。

    Args:
        product_id: 按产品 ID 筛选（可选）
        type_filter: 按类型筛选 requirement / test_case（可选）
        search: 按标题模糊搜索（可选）
        vectorized: 按向量化状态筛选 true/false（可选，仅对 requirement 有效）
        limit: 每页条数
        offset: 偏移量
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items（条目列表）/ total
    """
    items: list[dict] = []

    # 批量获取已向量化的 req_id 集合（避免 N+1 查询）
    vectorized_ids: set[str] = set()
    try:
        all_reqs = await asyncio.to_thread(vector_store.get_all_requirements, None, 1000)
        vectorized_ids = {r["id"] for r in all_reqs}
    except Exception as e:
        logger.warning("[API/knowledge] 获取向量库需求列表失败: {}", str(e)[:100])

    # 解析向量化筛选参数
    vectorized_filter: bool | None = None
    if vectorized is not None:
        if vectorized.lower() in ("true", "1", "yes"):
            vectorized_filter = True
        elif vectorized.lower() in ("false", "0", "no"):
            vectorized_filter = False

    # 从 PostgreSQL 读取已归档的需求（status='archived'）
    if not type_filter or type_filter == "requirement":
        stmt = select(Requirement).where(Requirement.status == "archived").order_by(Requirement.updated_at.desc())
        if product_id:
            stmt = stmt.where(Requirement.product_id == product_id)
        if search:
            stmt = stmt.where(Requirement.title.ilike(f"%{search}%"))
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        reqs = result.scalars().all()
        for r in reqs:
            is_vectorized = r.id in vectorized_ids
            # 向量化状态过滤
            if vectorized_filter is not None and is_vectorized != vectorized_filter:
                continue
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
                "archived_at": r.updated_at.isoformat() if r.updated_at else "",
                "go_live_date": r.go_live_date.isoformat() if r.go_live_date else "",
                "vectorized": is_vectorized,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            })

    # 从 PostgreSQL 读取测试用例
    if not type_filter or type_filter == "test_case":
        stmt = select(TestCase).order_by(TestCase.created_at.desc())
        if product_id:
            # 测试用例表自带 product_id 列，直接按产品隔离过滤
            stmt = stmt.where(TestCase.product_id == product_id)
        if search:
            stmt = stmt.where(TestCase.title.ilike(f"%{search}%"))
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        cases = result.scalars().all()
        for c in cases:
            items.append({
                "id": c.id,
                "title": c.title,
                "type": "test_case",
                "product_id": c.product_id,
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
    - test_case: 从 PG 删除 + 从向量库移除

    返回值包含 db_deleted（关系库）和 kb_deleted（向量库）分离状态，
    便于前端精确展示删除结果。
    """
    db_deleted = False
    kb_deleted = False
    kb_error = ""

    if entry_type == "requirement":
        # 恢复为 active 状态，退出知识库
        stmt = select(Requirement).where(Requirement.id == entry_id, Requirement.status == "archived")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail=f"需求 {entry_id} 不存在或未归档")
        record.status = "active"
        await session.commit()
        db_deleted = True
        logger.info("[API/knowledge] 需求退出知识库: id={}", entry_id)

        # 从向量库移除
        try:
            await asyncio.to_thread(vector_store.delete_requirement_from_kb, entry_id)
            kb_deleted = True
        except Exception as e:
            kb_error = str(e)[:300]
            logger.warning("[API/knowledge] 向量库删除失败: req_id={}, error={}", entry_id, kb_error[:200])

    elif entry_type == "test_case":
        # 从 PG 删除测试用例
        stmt = select(TestCase).where(TestCase.id == entry_id)
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()
        if not record:
            raise HTTPException(status_code=404, detail=f"测试用例 {entry_id} 不存在")
        await session.delete(record)
        await session.commit()
        db_deleted = True
        logger.info("[API/knowledge] 测试用例已删除: id={}", entry_id)

        # 从向量库移除
        try:
            await asyncio.to_thread(vector_store.delete_test_case_from_kb, entry_id)
            kb_deleted = True
        except Exception as e:
            kb_error = str(e)[:300]
            logger.warning("[API/knowledge] 向量库删除失败: case_id={}, error={}", entry_id, kb_error[:200])

    else:
        raise HTTPException(status_code=400, detail=f"不支持的条目类型: {entry_type}")

    message = f"已从知识库删除 {entry_type} {entry_id}"
    if db_deleted and not kb_deleted:
        message += "（向量库删除失败，请检查 Milvus 状态）"

    return ApiResponse(
        success=True,
        message=message,
        data={
            "entry_id": entry_id,
            "deleted": True,
            "db_deleted": db_deleted,
            "kb_deleted": kb_deleted,
            "kb_error": kb_error,
        },
    )


# ============================================================================
# 5. 知识库诊断（管理员）
# ============================================================================

@router.get("/diagnostics", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def get_kb_diagnostics() -> ApiResponse:
    """知识库诊断信息（管理员）。

    返回向量库连接状态、各 Collection 计数、嵌入模型状态、架构说明。
    供管理页面展示知识库健康度与组件角色（含 MinIO 说明）。
    """
    diag: dict = {
        "vector_db": {"connected": False, "model_ready": False, "collections": {}, "error": ""},
        "architecture": {
            "relational_db": "PostgreSQL — 存储需求/用例/模式元数据与 SRR JSON",
            "vector_db": "Milvus — 向量检索引擎，存储 1024 维语义向量",
            "object_storage": "MinIO — Milvus 的存储后端，存储向量数据文件（非独立 KB 管理器）",
            "coordination": "etcd — Milvus 的协调服务",
        },
    }
    try:
        diag["vector_db"]["connected"] = True
        model_status = await asyncio.to_thread(vector_store.model_status)
        diag["vector_db"]["model_ready"] = model_status["ready"]
        diag["vector_db"]["model_error"] = model_status["error"]
        diag["vector_db"]["collections"] = await asyncio.to_thread(vector_store.get_collection_stats)
    except Exception as e:
        diag["vector_db"]["error"] = str(e)[:200]
        logger.warning("[API/knowledge] 诊断失败: {}", str(e)[:200])

    return ApiResponse(
        success=True,
        message="ok",
        data=diag,
    )


# ============================================================================
# 3.1 嵌入模型就绪状态（供前端展示“为什么未向量化”）
# ============================================================================

@router.get("/model-status", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_model_status() -> ApiResponse:
    """返回嵌入模型就绪状态与诊断信息。

    前端在知识库页面据此展示横幅：模型未就绪时给出明确原因
    （如本地模型目录未挂载 / 权重缺失 / 需重启容器以重新建立 bind mount）。
    """
    status = await asyncio.to_thread(vector_store.model_status)
    return ApiResponse(success=True, message="ok", data=status)


# ============================================================================
# 3.2 重新向量化（补偿归档时写入失败 / Celery 曾宕机导致的历史欠账）
# ============================================================================

@router.post("/entries/{entry_id}/revectorize", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def reverctorize_requirement(entry_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """对单条已归档需求重新派发向量化任务。

    用于「已归档但未向量化」的需求补救。仅接受 status=archived 的需求。
    """
    stmt = select(Requirement).where(Requirement.id == entry_id, Requirement.status == "archived")
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {entry_id} 不存在或未归档")

    project_version = await _project_version(session, record.project_id)
    meta = _build_requirement_kb_meta(record, project_version)
    queued = _dispatch_embedding_task(record.id, record.srr_json or {}, meta)
    return ApiResponse(
        success=True,
        message="已重新派发向量化任务（异步）" if queued else "向量化任务派发失败：请检查 Celery 是否运行或嵌入模型是否就绪",
        data={"req_id": entry_id, "queued": queued},
    )


@router.post("/revectorize-missing", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def reverctorize_missing(session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """批量补向量化：对所有「已归档但未在 Milvus 中」的需求重新派发任务。

    返回统计：total_archived / missing / queued / failed。
    """
    # 1) 取 Milvus 已向量化集合
    # 注意：Milvus query 的 (offset+limit) 上限为 16384，超出会抛错。
    # get_all_requirements 不支持 offset 分页，故此处限额取 16384（需求规模远小于此，完全准确；
    # 极端超量时仅会多派发已向量化项，幂等无害）。
    vectorized_ids: set[str] = set()
    try:
        all_reqs = vector_store.get_all_requirements(None, 16384)
        vectorized_ids = {r["id"] for r in all_reqs}
    except Exception as e:
        logger.warning("[API/knowledge] 获取向量库列表失败: {}", str(e)[:100])

    # 2) 取全部已归档需求，筛选缺失项
    stmt = select(Requirement).where(Requirement.status == "archived")
    records = (await session.execute(stmt)).scalars().all()

    queued = 0
    failed = 0
    for r in records:
        if r.id in vectorized_ids:
            continue
        project_version = await _project_version(session, r.project_id)
        meta = _build_requirement_kb_meta(r, project_version)
        if _dispatch_embedding_task(r.id, r.srr_json or {}, meta):
            queued += 1
        else:
            failed += 1

    missing = len(records) - len(vectorized_ids)
    return ApiResponse(
        success=True,
        message=f"已补向量化 {queued} 条，失败 {failed} 条",
        data={
            "total_archived": len(records),
            "missing": max(missing, 0),
            "queued": queued,
            "failed": failed,
        },
    )
