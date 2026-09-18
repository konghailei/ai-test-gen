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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_admin, require_write
from app.utils.pagination import paginate
from app.db.database import get_session
from app.db.models import GenerationBatch, Project, Requirement, TestCase, TestPattern, KBSetting, LLMConfig
from app.db.milvus_store import vector_store
from app.models.api_schemas import ApiResponse, KnowledgeStats
from app.utils.logger import logger


router = APIRouter()


# ============================================================================
# 辅助：构建需求向量化元数据 + 派发异步嵌入任务
# ============================================================================

def _build_requirement_kb_meta(record: Requirement, project_version: str) -> dict:
    """为已归档需求构建 Milvus 写入元数据（与归档接口保持一致）。"""
    # v2.8.52: 补充 project_id 和 type 字段，与归档路径（products.py）保持一致。
    # 原实现缺失 project_id 导致项目删除/恢复活动时按 project_id 批量清理无法命中，
    # 孤儿向量残留并持续污染 KB 检索结果。
    return {
        "type": "requirement",
        "product_id": record.product_id,
        "project_id": record.project_id or "",
        "modules": ",".join(record.modules or []),
        "priority": record.priority,
        "status": "archived",
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


@router.get("/case-quality-report", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_case_quality_report(
    product_id: str | None = Query(default=None, description="按产品筛选"),
    top_n: int = Query(default=10, ge=1, le=50, description="Top-N 条数"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """用例库质量报表（docs/14 方向 B / B5）。

    四部分：三档占比 / 复用率 / weight 分布 / Top-N 复用与从未复用。

    ⚠️ **两处必须向用户说明的口径**（否则会误读）：

    1. **复用率会显著偏低**（实测 0.32%）。原因是 `execution_count`
       自 v3.3 才激活（用例被检索命中注入 prompt 时 +1），**历史复用未计入**。
       若不说明，用户会误判为"用例库完全没被使用"。报表用 `reuse.note` 显式标注。
    2. **weight 目前区分度弱**（实测 std 0.058，66% 落在 0.5 档）。
       原因是公式 4 个输入里 2 个尚无数据（`last_pass_rate` 全 0、
       `execution_count` 仅 5 条有值）。报表用 `weight.note` 标注，
       并给出分布直方图让用户自行判断。
    """
    def _f(stmt):
        return stmt.where(TestCase.product_id == product_id) if product_id else stmt

    # ---- ① 三档占比 ----
    status_rows = (await session.execute(
        _f(select(TestCase.status, func.count(TestCase.id))).group_by(TestCase.status)
    )).all()
    status_dist = {str(st): int(n) for st, n in status_rows}
    total = sum(status_dist.values())
    status_ratio = {k: round(v / total, 4) if total else 0.0 for k, v in status_dist.items()}

    # ---- ② 复用率 ----
    appr_total = status_dist.get("approved", 0)
    reused = (await session.execute(
        _f(select(func.count(TestCase.id))).where(
            TestCase.status == "approved", TestCase.execution_count > 0
        )
    )).scalar() or 0
    reuse_rate = round(reused / appr_total, 4) if appr_total else 0.0

    # ---- ③ weight 分布 ----
    w_rows = (await session.execute(
        _f(select(TestCase.weight, func.count(TestCase.id))).group_by(TestCase.weight)
    )).all()
    w_vals = [(float(w or 0.0), int(n)) for w, n in w_rows]
    w_total = sum(n for _, n in w_vals)
    w_mean = (sum(w * n for w, n in w_vals) / w_total) if w_total else 0.0
    w_var = (sum(n * (w - w_mean) ** 2 for w, n in w_vals) / w_total) if w_total else 0.0
    # 直方图按 0.1 分桶
    buckets: dict[float, int] = {}
    for w, n in w_vals:
        b = round(int(w * 10) / 10, 1)
        buckets[b] = buckets.get(b, 0) + n
    histogram = [
        {"bucket": f"{b:.1f}", "count": buckets[b],
         "ratio": round(buckets[b] / w_total, 4) if w_total else 0.0}
        for b in sorted(buckets)
    ]

    # ---- ④ Top-N 复用 / 从未复用 ----
    top_reused = [
        {"id": r[0], "title": r[1], "execution_count": int(r[2] or 0),
         "weight": round(float(r[3] or 0), 4), "status": r[4]}
        for r in (await session.execute(
            _f(select(TestCase.id, TestCase.title, TestCase.execution_count,
                      TestCase.weight, TestCase.status))
            .where(TestCase.execution_count > 0)
            .order_by(TestCase.execution_count.desc())
            .limit(top_n)
        )).all()
    ]
    never_reused_count = (await session.execute(
        _f(select(func.count(TestCase.id))).where(
            TestCase.status == "approved", TestCase.execution_count == 0
        )
    )).scalar() or 0
    never_reused_samples = [
        {"id": r[0], "title": r[1], "weight": round(float(r[2] or 0), 4)}
        for r in (await session.execute(
            _f(select(TestCase.id, TestCase.title, TestCase.weight))
            .where(TestCase.status == "approved", TestCase.execution_count == 0)
            .order_by(TestCase.weight.desc())
            .limit(top_n)
        )).all()
    ]

    logger.info(
        "[API/knowledge] 用例库质量报表: total={}, approved={}, 复用率={:.2%}, "
        "weight_mean={:.4f}, std={:.4f}",
        total, appr_total, reuse_rate, w_mean, w_var ** 0.5,
    )

    return ApiResponse(success=True, message="ok", data={
        "product_id": product_id,
        "total": total,
        "status_distribution": status_dist,
        "status_ratio": status_ratio,
        "reuse": {
            "approved_total": appr_total,
            "reused_count": reused,
            "reuse_rate": reuse_rate,
            # 对标值仅作参照，不参与任何判定
            "benchmark": 0.59,
            "note": (
                "`execution_count` 自 v3.3 起才激活（用例被检索命中并注入 prompt 时 +1），"
                "**历史复用未计入**。因此当前复用率会显著偏低，"
                "应看趋势而非绝对值。"
            ),
        },
        "weight": {
            "mean": round(w_mean, 4),
            "std": round(w_var ** 0.5, 4),
            "min": round(min((w for w, _ in w_vals), default=0.0), 4),
            "max": round(max((w for w, _ in w_vals), default=0.0), 4),
            "histogram": histogram,
            "note": (
                "weight 公式的 4 个输入中，`last_pass_rate` 尚无数据（全为中性 0.5）、"
                "`execution_count` 仅少量有值，因此**当前区分度较弱**"
                "（分布集中在 0.5 档）。待执行结果回流与使用量积累后自然改善，无需改代码。"
            ),
        },
        "top_reused": top_reused,
        "never_reused": {
            "count": never_reused_count,
            "samples": never_reused_samples,
        },
    })


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
    # 分页查询 —— v3.2.2 收敛
    stmt = select(TestPattern).order_by(TestPattern.usage_count.desc())
    if category:
        stmt = stmt.where(TestPattern.category == category)
    rows, total = await paginate(session, stmt, page, page_size)

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
    modules: str | None = Query(default=None, description="按模块筛选（逗号分隔，如 'auth,login'）"),
    limit: int = Query(default=100, ge=1, le=500, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识库中的条目列表。

    从 PostgreSQL 中读取已归档的需求和从向量库读取测试用例。
    支持按产品 ID、类型、标题搜索、向量化状态、模块筛选。

    Args:
        product_id: 按产品 ID 筛选（可选）
        type_filter: 按类型筛选 requirement / test_case（可选）
        search: 按标题模糊搜索（可选）
        vectorized: 按向量化状态筛选 true/false（可选，仅对 requirement 有效）
        modules: 按模块筛选（可选，逗号分隔的模块名，匹配条目任一所属模块即返回）
        limit: 每页条数
        offset: 偏移量
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items（条目列表）/ total
    """
    items: list[dict] = []

    # 解析模块筛选参数
    module_filter_set: set[str] = set()
    if modules:
        module_filter_set = {m.strip() for m in modules.split(",") if m.strip()}

    # 批量获取已向量化的 req_id 集合（避免 N+1 查询）
    vectorized_ids: set[str] = set()
    try:
        all_reqs = await asyncio.to_thread(vector_store.get_all_requirements, None, 16384)
        vectorized_ids = {r["id"] for r in all_reqs}
    except Exception as e:
        logger.warning("[API/knowledge] 获取向量库需求列表失败: {}", str(e)[:100])

    # 解析向量化筛选参数
    vectorized_filter: bool | str | None = None
    if vectorized is not None:
        if vectorized.lower() in ("true", "1", "yes"):
            vectorized_filter = True
        elif vectorized.lower() in ("false", "0", "no"):
            vectorized_filter = False
        elif vectorized.lower() in ("processing", "failed"):
            vectorized_filter = vectorized.lower()

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
            if isinstance(vectorized_filter, str):
                status_match = (r.vectorization_status or ("completed" if is_vectorized else None))
                if status_match != vectorized_filter:
                    continue
            elif vectorized_filter is not None and is_vectorized != vectorized_filter:
                continue
            # 模块过滤（任一所属模块命中筛选集合即返回，与 KB 问答的 modules 语义一致）
            if module_filter_set:
                req_modules = {m for m in (r.modules or []) if isinstance(m, str) and m.strip()}
                if not (req_modules & module_filter_set):
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
                # 真实归档时间，未设置时回退到 updated_at（兼容历史数据）
                "archived_at": (r.archived_at or r.updated_at).isoformat() if (r.archived_at or r.updated_at) else "",
                "go_live_date": r.go_live_date.isoformat() if r.go_live_date else "",
                "vectorized": is_vectorized,
                "vectorization_status": r.vectorization_status or ("completed" if is_vectorized else None),
                "vectorization_error": r.vectorization_error,
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
            # 模块过滤（任一所属模块命中筛选集合即返回）
            if module_filter_set:
                case_modules = {m for m in (c.modules or []) if isinstance(m, str) and m.strip()}
                if not (case_modules & module_filter_set):
                    continue
            items.append({
                "id": c.id,
                "title": c.title,
                "type": "test_case",
                "product_id": c.product_id,
                "modules": ",".join(c.modules or []),
                "priority": c.priority,
                "summary": f"类型: {c.type} / 优先级: {c.priority}" if c.type else "",
                # 真实归档时间，未设置时回退到 updated_at（兼容历史数据）
                "archived_at": (c.archived_at or c.updated_at).isoformat() if (c.archived_at or c.updated_at) else "",
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
        # v3.4.4（自审补漏 M-2）：提前留存 product_id（下方 commit 后 ORM 属性会失效），
        # 供向量删除后失效该产品 KB 缓存使用
        entry_product_id = record.product_id
        record.status = "active"
        await session.commit()
        db_deleted = True
        logger.info("[API/knowledge] 需求退出知识库: id={}", entry_id)

        # 从向量库移除（v3.4.4：传 product_id，删除后失效该产品 KB 检索缓存——
        # 否则退出知识库的需求在 24h TTL 内仍会被检索到）
        try:
            await asyncio.to_thread(
                vector_store.delete_requirement_from_kb, entry_id, entry_product_id
            )
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
# 4c. 知识库专属 LLM 配置
# ============================================================================

KB_LLM_SETTING_KEY = "llm_provider"


async def get_kb_llm_provider(session: AsyncSession) -> str | None:
    """读取知识库专属 LLM 供应商（供 knowledge_qa /ask 和 clarify_agent 使用）。

    返回 None 表示未设置，调用方应使用全局主 LLM。
    """
    try:
        result = await session.execute(
            select(KBSetting).where(KBSetting.key == KB_LLM_SETTING_KEY)
        )
        row = result.scalar_one_or_none()
        return row.value if row and row.value else None
    except Exception:
        return None


@router.get("/kb-llm", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_kb_llm(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识库专属 LLM 配置。

    返回当前 KB LLM 供应商 + 所有可用供应商列表（从 LLMConfig 表）。
    """
    # 当前 KB 专属 LLM
    current_provider = await get_kb_llm_provider(session)

    # 可用供应商列表
    result = await session.execute(
        select(LLMConfig).order_by(LLMConfig.is_primary.desc(), LLMConfig.id.desc())
    )
    rows = result.scalars().all()
    providers = [{
        "provider": r.provider,
        "provider_label": r.provider_label or r.provider,
        "model": r.model,
        "is_primary": r.is_primary,
    } for r in rows if r.api_key]  # 仅返回有 API Key 的

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "provider": current_provider or (providers[0]["provider"] if providers else ""),
            "providers": providers,
        },
    )


@router.put("/kb-llm", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def set_kb_llm(
    body: dict,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """设置知识库专属 LLM 供应商。

    body: {"provider": "deepseek"}
    """
    provider = body.get("provider", "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider 不能为空")

    # 验证供应商存在
    result = await session.execute(
        select(LLMConfig).where(LLMConfig.provider == provider)
    )
    llm_row = result.scalar_one_or_none()
    if not llm_row:
        raise HTTPException(status_code=404, detail=f"供应商 {provider} 不存在")

    # 写入 KBSetting
    existing = await session.execute(
        select(KBSetting).where(KBSetting.key == KB_LLM_SETTING_KEY)
    )
    row = existing.scalar_one_or_none()
    if row:
        row.value = provider
    else:
        session.add(KBSetting(key=KB_LLM_SETTING_KEY, value=provider))
    await session.commit()

    logger.info("[API/knowledge] KB LLM 已设置: provider={}", provider)

    return ApiResponse(
        success=True,
        message=f"知识库 LLM 已设置为 {llm_row.provider_label or provider}",
        data={"provider": provider},
    )


# ============================================================================
# 4d. 多模态 LLM 配置（v2.18: 图片/流程图识别专用）
# ============================================================================

VISION_LLM_SETTING_KEY = "vision_llm_provider"


async def get_vision_llm_provider(session: AsyncSession) -> str | None:
    """读取多模态 LLM 供应商（供 docx 图片识别使用）。

    返回 None 表示未设置，调用方应使用全局主 LLM（需主模型支持视觉）。
    """
    try:
        result = await session.execute(
            select(KBSetting).where(KBSetting.key == VISION_LLM_SETTING_KEY)
        )
        row = result.scalar_one_or_none()
        return row.value if row and row.value else None
    except Exception:
        return None


@router.get("/vision-llm", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_vision_llm(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取多模态 LLM 配置。

    返回当前视觉 LLM 供应商 + 所有可用供应商列表（从 LLMConfig 表）。
    多模态模型也可作为主模型，因此候选列表与 KB LLM 一致。
    """
    current_provider = await get_vision_llm_provider(session)

    result = await session.execute(
        select(LLMConfig).order_by(LLMConfig.is_primary.desc(), LLMConfig.id.desc())
    )
    rows = result.scalars().all()
    providers = [{
        "provider": r.provider,
        "provider_label": r.provider_label or r.provider,
        "model": r.model,
        "is_primary": r.is_primary,
    } for r in rows if r.api_key]

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "provider": current_provider or (providers[0]["provider"] if providers else ""),
            "providers": providers,
        },
    )


@router.put("/vision-llm", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def set_vision_llm(
    body: dict,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """设置多模态 LLM 供应商。

    body: {"provider": "qwen"} — 多模态模型也可作为主模型使用
    """
    provider = body.get("provider", "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider 不能为空")

    result = await session.execute(
        select(LLMConfig).where(LLMConfig.provider == provider)
    )
    llm_row = result.scalar_one_or_none()
    if not llm_row:
        raise HTTPException(status_code=404, detail=f"供应商 {provider} 不存在")

    existing = await session.execute(
        select(KBSetting).where(KBSetting.key == VISION_LLM_SETTING_KEY)
    )
    row = existing.scalar_one_or_none()
    if row:
        row.value = provider
    else:
        session.add(KBSetting(key=VISION_LLM_SETTING_KEY, value=provider))
    await session.commit()

    logger.info("[API/knowledge] 多模态 LLM 已设置: provider={}", provider)

    return ApiResponse(
        success=True,
        message=f"多模态 LLM 已设置为 {llm_row.provider_label or provider}",
        data={"provider": provider},
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
        "warnings": [],
    }

    # 检测 zhparser 中文分词
    try:
        from sqlalchemy import text as sa_text
        async for tmp_session in get_session():
            ts = await tmp_session.execute(sa_text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'"))
            if not ts.fetchone():
                diag["warnings"].append(
                    "zhparser 中文分词未安装：BM25 全文检索使用 simple 配置，中文分词效果有限。"
                    "建议在 PostgreSQL 容器中安装 postgresql-16-zhparser 并重启。"
                )
            break
    except Exception as e:
        logger.warning("[API/knowledge] zhparser 配置诊断失败: {}", str(e)[:150])
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

    # 标记向量化状态为"进行中"，清除旧错误
    record.vectorization_status = "processing"
    record.vectorization_error = None
    await session.commit()

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
            r.vectorization_status = "processing"
            r.vectorization_error = None
            queued += 1
        else:
            failed += 1
    await session.commit()

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
