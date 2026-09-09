"""测试用例管理路由。

提供用例的列表查询、详情查看、编辑、删除与反馈回流。

端点清单（实现计划 T5.2）：
    GET    /api/testcases              用例列表（筛选 + 分页）
    GET    /api/testcases/{id}         用例详情
    PUT    /api/testcases/{id}         编辑用例
    DELETE /api/testcases/{id}         删除用例
    POST   /api/testcases/{id}/feedback  反馈回流（采纳/拒绝 + 评论）

设计依据：
- docs/00-architecture-overview.md §2 访问层
- docs/03-case-generation-flow.md §5（ReviewAgent 审核后用例管理）
- docs/04-output-format.md §1（UTCF 用例格式）

反馈回流说明：
    用户对 AI 生成的用例进行人工复核后，可通过 feedback 端点提交反馈。
    反馈信息存入 TestCase.feedback 字段，同时更新 status（approved/rejected），
    供后续模型效果分析与知识库优化使用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user, require_admin, require_write
from app.utils.pagination import paginate
from app.core.product_access import (
    apply_visible_product_filter,
    assert_product_visible,
    get_visible_product_ids,
)
from app.db.database import get_session
from app.db.models import TestCase, Traceability, ErrorCase
from app.db.milvus_store import vector_store
from app.models.api_schemas import ApiResponse, FeedbackRequest, TestCaseCreateRequest, TestCaseUpdateRequest
from app.utils.logger import logger


def _extract_fp(case_json: dict | None) -> str:
    """从 case_json 中提取功能点名称。"""
    if not case_json:
        return ""
    return case_json.get("related_fp", "") or case_json.get("functional_point", "") or ""


router = APIRouter()


# ============================================================================
# 1. 用例列表（筛选 + 分页）
# ============================================================================

@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_testcases(
    req_id: str | None = Query(default=None, description="按需求 ID 筛选"),
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    project_id: str | None = Query(default=None, description="按项目 ID 筛选"),
    search: str | None = Query(default=None, description="标题模糊检索"),
    case_type: str | None = Query(default=None, description="按用例类型筛选 functional/boundary/exception/security"),
    priority: str | None = Query(default=None, description="按优先级筛选 P0-P3"),
    status: str | None = Query(default=None, description="按状态筛选 draft/reviewed/approved/rejected"),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0, description="最低置信度"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """获取测试用例列表（支持多维度筛选 + 分页）。

    筛选条件：req_id、product_id、project_id、case_type、priority、status、min_confidence
    分页：page / page_size，按创建时间倒序
    """
    # 构建查询条件
    conditions = []
    # v3.1.3 产品可见性：普通用户仅能看到 visible_to_user_ids 命中（或名单为空）的产品
    visible_ids = await get_visible_product_ids(session, user)
    apply_visible_product_filter(conditions, visible_ids, TestCase.product_id)
    if req_id:
        conditions.append(TestCase.req_id == req_id)
    if product_id:
        conditions.append(TestCase.product_id == product_id)
    if project_id:
        # 按项目筛选：先找出该项目下所有需求，再按需求ID查用例
        from app.db.models import Requirement
        if project_id == "unassigned":
            # 筛选未绑定项目的需求对应的用例
            conditions.append(
                TestCase.req_id.in_(
                    select(Requirement.id).where(Requirement.project_id.is_(None))
                )
            )
        else:
            conditions.append(
                TestCase.req_id.in_(
                    select(Requirement.id).where(Requirement.project_id == project_id)
                )
            )
    if case_type:
        conditions.append(TestCase.type == case_type)
    if priority:
        conditions.append(TestCase.priority == priority)
    if status:
        conditions.append(TestCase.status == status)
    if min_confidence is not None:
        conditions.append(TestCase.confidence >= min_confidence)
    if search:
        conditions.append(TestCase.title.ilike(f"%{search}%"))

    # 分页查询（按创建时间倒序）—— v3.2.2 收敛：count+分页统一走 utils.pagination
    stmt = select(TestCase).order_by(TestCase.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    rows, total = await paginate(session, stmt, page, page_size)

    # 批量查询功能点名称（fp_id → fp_name 映射），避免前端显示 fp_id
    from app.db.models import FunctionalPoint, Requirement as ReqModel, Project
    fp_ids = set()
    req_ids = set()
    for r in rows:
        fp_id = _extract_fp(r.case_json)
        if fp_id and fp_id != "业务场景":
            fp_ids.add(fp_id)
        req_ids.add(r.req_id)
    fp_name_map: dict[str, str] = {}
    req_title_map: dict[str, str] = {}
    req_project_map: dict[str, str] = {}  # req_id → project_id
    if fp_ids:
        fp_rows = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(fp_ids))
        )
        fp_name_map = {fid: fname for fid, fname in fp_rows.all()}
    if req_ids:
        req_rows = await session.execute(
            select(ReqModel.id, ReqModel.title, ReqModel.project_id).where(ReqModel.id.in_(req_ids))
        )
        for rid, rtitle, pid in req_rows.all():
            req_title_map[rid] = rtitle
            if pid:
                req_project_map[rid] = pid
    # 批量查询项目名称（project_id → project_name 映射）
    project_name_map: dict[str, str] = {}
    project_ids = set(req_project_map.values())
    if project_ids:
        proj_rows = await session.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        project_name_map = {pid: pname for pid, pname in proj_rows.all()}

    items = [
        {
            "id": r.id,
            "req_id": r.req_id,
            "req_title": req_title_map.get(r.req_id, r.req_id),
            "project_name": project_name_map.get(req_project_map.get(r.req_id, ""), ""),
            "product_id": r.product_id,
            "title": r.title,
            "type": r.type,
            "priority": r.priority,
            "status": r.status,
            "confidence": r.confidence,
            "gen_batch_id": r.gen_batch_id,
            "functional_point": fp_name_map.get(_extract_fp(r.case_json), _extract_fp(r.case_json)),
            "description": (r.case_json or {}).get("description", ""),
            "preconditions": (r.case_json or {}).get("preconditions", []),
            "steps": (r.case_json or {}).get("steps", []),
            "created_at": r.created_at.isoformat() if r.created_at else "",
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
# 2. 用例详情
# ============================================================================

@router.get("/{case_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_testcase(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """获取测试用例详情（含完整 UTCF case_json）。

    Args:
        case_id: 用例 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含用例元数据 + case_json（完整 UTCF）

    Raises:
        HTTPException 404: 用例不存在
        HTTPException 403: 用例所属产品对当前用户不可见
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

    # v3.1.3 产品可见性校验
    await assert_product_visible(session, user, record.product_id)

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "id": record.id,
            "req_id": record.req_id,
            "product_id": record.product_id,
            "title": record.title,
            "type": record.type,
            "priority": record.priority,
            "status": record.status,
            "confidence": record.confidence,
            "case_json": record.case_json,
            "source": record.source,
            "gen_batch_id": record.gen_batch_id,
            "feedback": record.feedback,
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "updated_at": record.updated_at.isoformat() if record.updated_at else "",
        },
    )


# ============================================================================
# 3. 编辑用例
# ============================================================================

@router.post("", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def create_testcase(
    req: TestCaseCreateRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """人工新增测试用例。

    必须关联到具体需求（req_id）；若未传 product_id，则从需求记录自动推导。
    生成 ID 格式：TC-{REQ后缀}-{时间戳后6位}，避免与 AI 生成用例冲突。
    """
    import time
    from app.db.models import Requirement

    # 1) 校验需求存在
    req_row = await session.get(Requirement, req.req_id)
    if req_row is None:
        raise HTTPException(status_code=404, detail=f"需求 {req.req_id} 不存在，无法创建用例")

    # 2) 推导 product_id
    product_id = req.product_id or req_row.product_id or ""

    # v3.1.3 产品可见性校验（目标产品对当前用户不可见则拒绝写入）
    await assert_product_visible(session, user, product_id)

    # 3) 生成用例 ID
    req_suffix = req.req_id.split("-")[-1] if "-" in req.req_id else req.req_id[:6]
    ts_suffix = int(time.time()) % 1000000
    case_id = f"TC-{req_suffix}-{ts_suffix}"

    # 4) 构造 case_json
    case_json = {
        "title": req.title,
        "type": req.type,
        "priority": req.priority,
        "description": req.description,
        "preconditions": req.preconditions or [],
        "steps": req.steps or [],
        "expected": req.expected,
        "assertions": req.assertions or [],
        "source": "manual",
    }

    # 5) 写入
    record = TestCase(
        id=case_id,
        req_id=req.req_id,
        product_id=product_id,
        title=req.title,
        type=req.type,
        priority=req.priority,
        status="draft",
        confidence=0.0,
        case_json=case_json,
        source="manual",
    )
    session.add(record)
    await session.commit()

    logger.info(
        "[API/testcases] 用例已创建: case_id={}, req_id={}, title={}",
        case_id, req.req_id, req.title[:50],
    )

    return ApiResponse(
        success=True,
        message="用例已创建",
        data={
            "id": case_id,
            "req_id": req.req_id,
            "product_id": product_id,
            "title": record.title,
            "type": record.type,
            "priority": record.priority,
            "status": record.status,
        },
    )


@router.put("/{case_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_testcase(
    case_id: str,
    req: TestCaseUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """编辑测试用例（仅更新提供的字段，支持部分更新）。

    编辑后会同步更新 case_json 中对应的字段，保持元数据与 case_json 一致。

    Args:
        case_id: 用例 ID
        req: 更新请求（所有字段可选）
        session: 数据库会话

    Returns:
        ApiResponse: data 含更新后的用例摘要

    Raises:
        HTTPException 404: 用例不存在 / 目标需求不存在
        HTTPException 403: 用例（或目标需求）所属产品对当前用户不可见
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

    # v3.1.3 产品可见性校验
    await assert_product_visible(session, user, record.product_id)

    # 更新元数据字段
    updated_fields: list[str] = []
    if req.title is not None:
        record.title = req.title
        updated_fields.append("title")
    if req.type is not None:
        record.type = req.type
        updated_fields.append("type")
    if req.priority is not None:
        record.priority = req.priority
        updated_fields.append("priority")
    if req.req_id is not None and req.req_id != record.req_id:
        # v3.1.3 修复：变更所属需求必须校验目标需求存在，并同步 product_id，
        # 避免用例指向另一产品的需求却保留旧 product_id（隔离错乱 + 孤立数据）
        from app.db.models import Requirement

        new_req = await session.get(Requirement, req.req_id)
        if new_req is None:
            raise HTTPException(status_code=404, detail=f"目标需求 {req.req_id} 不存在")
        await assert_product_visible(session, user, new_req.product_id)
        record.req_id = req.req_id
        record.product_id = new_req.product_id or record.product_id
        updated_fields.append("req_id")

    # 更新 case_json 中的内容字段
    case_json = dict(record.case_json or {})
    json_fields = [
        "description", "preconditions", "steps", "expected", "test_data", "assertions",
    ]
    for field in json_fields:
        value = getattr(req, field)
        if value is not None:
            case_json[field] = value
            updated_fields.append(field)

    # 更新 functional_point（存储在 case_json 中）
    if req.functional_point is not None:
        case_json["related_fp"] = req.functional_point
        case_json["functional_point"] = req.functional_point
        updated_fields.append("functional_point")

    # 同步 title/type/priority 到 case_json
    if req.title is not None:
        case_json["title"] = req.title
    if req.type is not None:
        case_json["type"] = req.type
    if req.priority is not None:
        case_json["priority"] = req.priority

    record.case_json = case_json
    # 仅当内容字段变更时才标记为已审核；拖拽移动（仅 functional_point/req_id）不改状态
    content_fields = {"title", "type", "priority", "description", "preconditions",
                      "steps", "expected", "test_data", "assertions"}
    if set(updated_fields) & content_fields:
        record.status = "reviewed"
    await session.commit()

    logger.info(
        "[API/testcases] 用例已更新: case_id={}, fields={}",
        case_id,
        updated_fields,
    )

    return ApiResponse(
        success=True,
        message="用例已更新",
        data={
            "id": record.id,
            "title": record.title,
            "type": record.type,
            "priority": record.priority,
            "status": record.status,
            "updated_fields": updated_fields,
        },
    )


# ============================================================================
# 4. 批量删除（按需求）
# ============================================================================

@router.delete("/batch/by-requirement/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def batch_delete_by_requirement(
    req_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """按需求批量删除所有测试用例。"""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select as sa_select

    # P1-1 产品隔离：取该需求下任一用例的 product_id 校验可见性
    probe = await session.execute(
        sa_select(TestCase.product_id).where(TestCase.req_id == req_id).limit(1)
    )
    probe_pid = probe.scalar_one_or_none()
    if probe_pid:
        await assert_product_visible(session, user, probe_pid)

    # 先删该需求下所有用例的追溯记录
    tc_ids_subquery = sa_select(TestCase.id).where(TestCase.req_id == req_id)
    await session.execute(
        sa_delete(Traceability).where(Traceability.test_case_id.in_(tc_ids_subquery))
    )
    # 级联删除 error_cases 表中引用这些用例的记录（外键 RESTRICT，必须先删）
    await session.execute(
        sa_delete(ErrorCase).where(ErrorCase.original_case_id.in_(tc_ids_subquery))
    )
    # 再删用例
    result = await session.execute(sa_delete(TestCase).where(TestCase.req_id == req_id))
    await session.commit()

    # 同步删除 Milvus 向量（失败不阻断主流程）
    try:
        import asyncio
        await asyncio.to_thread(vector_store.delete_cases_by_req_id, req_id)
    except Exception as e:
        logger.warning("[API/testcases] Milvus 向量批量删除失败（不阻断）: req_id={}, err={}", req_id, str(e)[:120])

    logger.info("[API/testcases] 批量删除: req_id={}, deleted={}", req_id, result.rowcount)
    return ApiResponse(success=True, data={"deleted_count": result.rowcount})


# ============================================================================
# 5. 删除单个用例
# ============================================================================

@router.delete("/{case_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def delete_testcase(
    case_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """删除测试用例。

    Args:
        case_id: 用例 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 deleted_id

    Raises:
        HTTPException 404: 用例不存在
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

    # P1-1 产品隔离校验
    await assert_product_visible(session, user, record.product_id)

    # 先删追溯记录
    from sqlalchemy import delete as _del
    await session.execute(_del(Traceability).where(Traceability.test_case_id == case_id))
    # 级联删除 error_cases 表中引用该用例的记录（外键 RESTRICT，必须先删）
    await session.execute(_del(ErrorCase).where(ErrorCase.original_case_id == case_id))
    await session.delete(record)
    await session.commit()

    # 同步删除 Milvus 向量（失败不阻断主流程）
    try:
        import asyncio
        await asyncio.to_thread(vector_store.delete_test_case, case_id)
    except Exception as e:
        logger.warning("[API/testcases] Milvus 向量删除失败（不阻断）: case_id={}, err={}", case_id, str(e)[:120])

    logger.info("[API/testcases] 用例已删除: case_id={}", case_id)

    return ApiResponse(
        success=True,
        message="用例已删除",
        data={"deleted_id": case_id},
    )


# ============================================================================
# 5. 批量审核（一键审核目录下所有用例）— 必须在 /{case_id}/feedback 之前
# ============================================================================

class BatchFeedbackRequest(BaseModel):
    """批量审核请求。"""
    case_ids: list[str] = Field(..., min_length=1, description="用例 ID 列表")
    accepted: bool = Field(True, description="True=批量通过，False=批量驳回")
    comments: str = Field("", description="审核意见")


@router.post("/batch/feedback", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def batch_feedback(
    req: BatchFeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """批量审核用例。

    对指定的多个用例执行批量审核（通过/驳回），更新 status 与 feedback 字段。

    Args:
        req: 批量审核请求（case_ids + accepted + comments）
        user: 当前用户
        session: 数据库会话

    Returns:
        ApiResponse: data 含 updated_count / new_status
    """
    new_status = "approved" if req.accepted else "rejected"
    feedback_data = {
        "accepted": req.accepted,
        "comments": req.comments or ("批量审核通过" if req.accepted else "批量审核驳回"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "batch": True,
    }

    stmt = select(TestCase).where(TestCase.id.in_(req.case_ids))
    result = await session.execute(stmt)
    records = result.scalars().all()

    # P1-1 产品隔离校验：任一用例不可见即整体拒绝（fail-fast，避免部分提交）
    for record in records:
        await assert_product_visible(session, user, record.product_id)

    updated_count = 0
    for record in records:
        record.status = new_status
        record.feedback = feedback_data
        record.reviewed_by = user.username
        record.reviewed_at = datetime.now(timezone.utc)
        updated_count += 1

    await session.commit()

    # 审核通过时同步写入 Milvus 向量，建立回归检索基线。
    # 测试发现原实现只更新 PG 不写向量，导致飞轮冷启动时必须手动逐条
    # 调用 vector_store.add_test_case 才能建立基线。此修复使批量审批
    # 接口成为 PG + Milvus 的双写端点，rejected 时同步删除向量。
    milvus_written = 0
    milvus_failed = 0
    if req.accepted:
        import asyncio
        for record in records:
            try:
                case_json = record.case_json or {}
                case_json.setdefault("id", record.id)
                await asyncio.to_thread(
                    vector_store.add_test_case,
                    case_id=record.id,
                    case_json=case_json,
                    metadata={
                        "req_id": record.req_id,
                        "product_id": record.product_id,
                        "case_type": record.type,
                        "confidence": str(record.confidence),
                        "gen_batch_id": record.gen_batch_id or "",
                    },
                )
                milvus_written += 1
            except Exception as e:
                milvus_failed += 1
                logger.warning(
                    "[API/testcases] 批量审批 Milvus 写入失败: case_id={}, err={}",
                    record.id, str(e)[:100],
                )
    else:
        import asyncio
        for record in records:
            try:
                await asyncio.to_thread(
                    vector_store.delete_test_case, record.id
                )
            except Exception as e:
                logger.warning(
                    "[API/testcases] 批量驳回 Milvus 删除失败: case_id={}, err={}",
                    record.id, str(e)[:100],
                )

    logger.info(
        "[API/testcases] 批量审核: count={}, accepted={}, by={}, milvus_written={}, milvus_failed={}",
        updated_count, req.accepted, user.username, milvus_written, milvus_failed,
    )

    return ApiResponse(
        success=True,
        message=f"已批量{'通过' if req.accepted else '驳回'} {updated_count} 个用例",
        data={
            "updated_count": updated_count,
            "new_status": new_status,
            "accepted": req.accepted,
            "milvus_written": milvus_written,
            "milvus_failed": milvus_failed,
        },
    )


# ============================================================================
# 6. 反馈回流（单条）
# ============================================================================

@router.post("/{case_id}/feedback", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def submit_feedback(
    case_id: str,
    req: FeedbackRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """提交用例审核反馈（采纳/拒绝 + 评论）。

    反馈信息存入 TestCase.feedback 字段，同时更新 status：
        - accepted=True → status="approved"
        - accepted=False → status="rejected"

    这些反馈数据用于后续模型效果分析与知识库优化。

    Args:
        case_id: 用例 ID
        req: 反馈请求，含 accepted（是否采纳）和 comments（评论）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 case_id / new_status

    Raises:
        HTTPException 404: 用例不存在
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

    # P1-1 产品隔离校验
    await assert_product_visible(session, user, record.product_id)

    # 更新反馈与状态
    new_status = "approved" if req.accepted else "rejected"
    record.status = new_status
    record.feedback = {
        "accepted": req.accepted,
        "comments": req.comments,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    record.reviewed_by = user.username
    record.reviewed_at = datetime.now(timezone.utc)
    await session.commit()

    # 同步 Milvus 向量：approved 时写入/reupsert，rejected 时删除。
    # 与 batch_feedback 保持一致，保证单条审核也完成 PG + Milvus 双写。
    milvus_ok = True
    if req.accepted:
        import asyncio
        try:
            case_json = record.case_json or {}
            case_json.setdefault("id", case_id)
            await asyncio.to_thread(
                vector_store.add_test_case,
                case_id=case_id,
                case_json=case_json,
                metadata={
                    "req_id": record.req_id,
                    "product_id": record.product_id,
                    "case_type": record.type,
                    "confidence": str(record.confidence),
                    "gen_batch_id": record.gen_batch_id or "",
                },
            )
        except Exception as e:
            milvus_ok = False
            logger.warning(
                "[API/testcases] 单条审批 Milvus 写入失败: case_id={}, err={}",
                case_id, str(e)[:100],
            )
    else:
        import asyncio
        try:
            await asyncio.to_thread(vector_store.delete_test_case, case_id)
        except Exception as e:
            milvus_ok = False
            logger.warning(
                "[API/testcases] 单条驳回 Milvus 删除失败: case_id={}, err={}",
                case_id, str(e)[:100],
            )

    logger.info(
        "[API/testcases] 反馈已记录: case_id={}, accepted={}, status={}, milvus_ok={}",
        case_id,
        req.accepted,
        new_status,
        milvus_ok,
    )

    return ApiResponse(
        success=True,
        message="反馈已记录",
        data={
            "case_id": case_id,
            "new_status": new_status,
            "accepted": req.accepted,
            "milvus_synced": milvus_ok,
        },
    )
