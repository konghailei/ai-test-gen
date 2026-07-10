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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user, require_admin, require_write
from app.db.database import get_session
from app.db.models import TestCase
from app.models.api_schemas import ApiResponse, FeedbackRequest, TestCaseUpdateRequest
from app.utils.logger import logger


router = APIRouter()


# ============================================================================
# 1. 用例列表（筛选 + 分页）
# ============================================================================

@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_testcases(
    req_id: str | None = Query(default=None, description="按需求 ID 筛选"),
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    case_type: str | None = Query(default=None, description="按用例类型筛选 functional/boundary/exception/security"),
    priority: str | None = Query(default=None, description="按优先级筛选 P0-P3"),
    status: str | None = Query(default=None, description="按状态筛选 draft/reviewed/approved/rejected"),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0, description="最低置信度"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取测试用例列表（支持多维度筛选 + 分页）。

    Args:
        req_id: 按需求 ID 筛选
        product_id: 按产品 ID 筛选
        case_type: 按用例类型筛选
        priority: 按优先级筛选
        status: 按状态筛选
        min_confidence: 最低置信度阈值
        page: 页码
        page_size: 每页条数
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items / total / page / page_size
    """
    # 构建查询条件
    conditions = []
    if req_id:
        conditions.append(TestCase.req_id == req_id)
    if product_id:
        conditions.append(TestCase.product_id == product_id)
    if case_type:
        conditions.append(TestCase.type == case_type)
    if priority:
        conditions.append(TestCase.priority == priority)
    if status:
        conditions.append(TestCase.status == status)
    if min_confidence is not None:
        conditions.append(TestCase.confidence >= min_confidence)

    # 计算总数
    count_stmt = select(func.count(TestCase.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await session.execute(count_stmt)).scalar() or 0

    # 分页查询（按创建时间倒序）
    stmt = select(TestCase).order_by(TestCase.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        {
            "id": r.id,
            "req_id": r.req_id,
            "product_id": r.product_id,
            "title": r.title,
            "type": r.type,
            "priority": r.priority,
            "status": r.status,
            "confidence": r.confidence,
            "gen_batch_id": r.gen_batch_id,
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
) -> ApiResponse:
    """获取测试用例详情（含完整 UTCF case_json）。

    Args:
        case_id: 用例 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含用例元数据 + case_json（完整 UTCF）

    Raises:
        HTTPException 404: 用例不存在
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

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

@router.put("/{case_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_testcase(
    case_id: str,
    req: TestCaseUpdateRequest,
    session: AsyncSession = Depends(get_session),
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
        HTTPException 404: 用例不存在
    """
    stmt = select(TestCase).where(TestCase.id == case_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"用例 {case_id} 不存在")

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

    # 更新 case_json 中的内容字段
    case_json = dict(record.case_json or {})
    json_fields = [
        "preconditions", "steps", "expected", "test_data", "assertions",
    ]
    for field in json_fields:
        value = getattr(req, field)
        if value is not None:
            case_json[field] = value
            updated_fields.append(field)

    # 同步 title/type/priority 到 case_json
    if req.title is not None:
        case_json["title"] = req.title
    if req.type is not None:
        case_json["type"] = req.type
    if req.priority is not None:
        case_json["priority"] = req.priority

    record.case_json = case_json
    record.status = "reviewed"  # 人工编辑后标记为已审核
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
# 4. 删除用例
# ============================================================================

@router.delete("/{case_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_testcase(
    case_id: str,
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

    await session.delete(record)
    await session.commit()

    logger.info("[API/testcases] 用例已删除: case_id={}", case_id)

    return ApiResponse(
        success=True,
        message="用例已删除",
        data={"deleted_id": case_id},
    )


# ============================================================================
# 5. 反馈回流
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

    logger.info(
        "[API/testcases] 反馈已记录: case_id={}, accepted={}, status={}",
        case_id,
        req.accepted,
        new_status,
    )

    return ApiResponse(
        success=True,
        message="反馈已记录",
        data={
            "case_id": case_id,
            "new_status": new_status,
            "accepted": req.accepted,
        },
    )
