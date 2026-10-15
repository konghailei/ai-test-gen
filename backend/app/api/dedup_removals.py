"""去重留痕查询与一键恢复路由（v3.5.10 docs/32 B-6 闸门3）。

端点清单：
    GET  /api/dedup-removals                留痕列表（按任务/批次/decision/恢复状态筛选）
    GET  /api/dedup-removals/{removal_id}   留痕详情（含被去重用例完整 JSON）
    POST /api/dedup-removals/{removal_id}/restore  误删一键恢复（写回 test_cases）

设计（docs/32 §B-6 闸门3）：0.89-0.95 区间误删/漏判分布重叠是结构性的，
任何判定都可能有残余误差——留痕 + 一键恢复把"误删"从不可逆事故降级为
可纠错操作（error_cases 复活同型 UI 先例）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user, require_write
from app.db.database import get_session
from app.db.models import DedupRemoval, Requirement, TestCase
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

router = APIRouter()


def _row_to_dict(row: DedupRemoval, include_case: bool = False) -> dict:
    data = {
        "removal_id": row.removal_id,
        "task_id": row.task_id,
        "gen_batch_id": row.gen_batch_id,
        "req_id": row.req_id,
        "decision": row.decision,
        "removed_id": row.removed_id,
        "removed_title": row.removed_title,
        "kept_id": row.kept_id,
        "kept_title": row.kept_title,
        "reason": row.reason,
        "restored": row.restored,
        "restored_case_id": row.restored_case_id,
        "restored_at": row.restored_at.isoformat() if row.restored_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if include_case:
        data["removed_case_json"] = row.removed_case_json or {}
    return data


@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_dedup_removals(
    task_id: str | None = Query(default=None, description="按生成任务 ID 筛选"),
    gen_batch_id: str | None = Query(default=None, description="按生成批次筛选"),
    req_id: str | None = Query(default=None, description="按需求 ID 筛选"),
    decision: str | None = Query(default=None, description="removed / kept"),
    restored: bool | None = Query(default=None, description="按恢复状态筛选"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """去重留痕列表（分页，按创建时间倒序）。"""
    conditions = []
    if task_id:
        conditions.append(DedupRemoval.task_id == task_id)
    if gen_batch_id:
        conditions.append(DedupRemoval.gen_batch_id == gen_batch_id)
    if req_id:
        conditions.append(DedupRemoval.req_id == req_id)
    if decision:
        conditions.append(DedupRemoval.decision == decision)
    if restored is not None:
        conditions.append(DedupRemoval.restored.is_(restored))

    stmt = select(DedupRemoval).where(*conditions) if conditions else select(DedupRemoval)
    total = (await session.execute(
        select(DedupRemoval.id).where(*conditions)
        if conditions else select(DedupRemoval.id)
    )).scalars().all()
    rows = (await session.execute(
        stmt.order_by(DedupRemoval.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    return ApiResponse(
        success=True,
        message="查询成功",
        data={
            "total": len(total),
            "page": page,
            "page_size": page_size,
            "items": [_row_to_dict(r) for r in rows],
        },
    )


@router.get("/{removal_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_dedup_removal(
    removal_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """留痕详情（含被去重用例完整 JSON，供人工比对与恢复预览）。"""
    row = (await session.execute(
        select(DedupRemoval).where(DedupRemoval.removal_id == removal_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"留痕 {removal_id} 不存在")
    return ApiResponse(success=True, message="查询成功", data=_row_to_dict(row, include_case=True))


@router.post(
    "/{removal_id}/restore",
    response_model=ApiResponse,
    dependencies=[Depends(require_write)],
)
async def restore_dedup_removal(
    removal_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """误删一键恢复：把 removed_case_json 写回 test_cases（error_cases 复活同型）。

    幂等：已恢复的留痕再次调用直接返回既有 restored_case_id，不重复插入。
    恢复标记链路：restored=True + restored_case_id + restored_at（审计闭环）。
    """
    row = (await session.execute(
        select(DedupRemoval).where(DedupRemoval.removal_id == removal_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"留痕 {removal_id} 不存在")
    if row.decision != "removed":
        raise HTTPException(status_code=400, detail="仅 removed（剔除）记录可恢复")
    if row.restored:
        return ApiResponse(
            success=True,
            message="该记录已恢复过，不重复插入",
            data={"removal_id": removal_id, "restored_case_id": row.restored_case_id},
        )

    case_json = row.removed_case_json or {}
    if not case_json:
        raise HTTPException(status_code=400, detail="留痕未存完整用例 JSON，无法恢复")

    req_id = row.req_id or case_json.get("requirement_id") or ""
    req_row = await session.get(Requirement, req_id) if req_id else None
    if req_row is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在，无法恢复用例")

    from app.utils.id_gen import gen_test_case_id
    new_id = gen_test_case_id(module_prefix="RESTORED")

    record = TestCase(
        id=new_id,
        req_id=req_id,
        product_id=req_row.product_id or "PROD-DEFAULT",
        title=str(case_json.get("title") or row.removed_title or "（恢复用例）")[:512],
        type=str(case_json.get("type") or "functional"),
        priority=str(case_json.get("priority") or "P1"),
        status="draft",
        confidence=float(case_json.get("confidence") or 0.0),
        case_json=case_json,
        source="dedup_restored",
        gen_batch_id=row.gen_batch_id,
    )
    session.add(record)
    row.restored = True
    row.restored_case_id = new_id
    row.restored_at = datetime.now(timezone.utc)
    await session.commit()

    logger.info(
        "[API/dedup-removals] 误删用例已恢复: removal_id={}, new_case_id={}, title={}",
        removal_id, new_id, record.title[:50],
    )
    return ApiResponse(
        success=True,
        message="用例已恢复",
        data={"removal_id": removal_id, "restored_case_id": new_id, "req_id": req_id},
    )
