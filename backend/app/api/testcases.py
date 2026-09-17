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
from app.db.models import TestCase, Traceability, ErrorCase, Requirement
from app.db.milvus_store import vector_store
from app.models.api_schemas import ApiResponse, FeedbackRequest, TestCaseCreateRequest, TestCaseUpdateRequest
from app.utils.logger import logger


# v3.4.1 修复 P-5：无功能点归属用例的分组哨兵值（用例生成链路的约定值）。
# 此前散落为裸字符串字面量，且部分调用点未做过滤，会把哨兵值原样回显给前端。
FP_SENTINEL_UNASSIGNED = "业务场景"


def _extract_fp(case_json: dict | None) -> str:
    """从 case_json 中提取功能点名称。"""
    if not case_json:
        return ""
    return case_json.get("related_fp", "") or case_json.get("functional_point", "") or ""


def _fp_display_name(case_json: dict | None, fp_name_map: dict) -> str:
    """用例的功能点展示名：哨兵值不展示，映射不到时回退原值。"""
    fp = _extract_fp(case_json)
    if not fp or fp == FP_SENTINEL_UNASSIGNED:
        return ""
    return fp_name_map.get(fp, fp)


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
        if fp_id and fp_id != FP_SENTINEL_UNASSIGNED:
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
            "functional_point": _fp_display_name(r.case_json, fp_name_map),
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
    用例 ID 由 id_gen.gen_test_case_id() 生成（TC-MANUAL-{批次}-{序号}）；
    v3.4.1 起不再使用秒级时间戳（同秒重复提交会撞主键）。
    """
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
    # v3.4.1 修复 H-2：改用 gen_test_case_id()（100 秒分桶 + 递增序号 + 线程锁）。
    # 原实现 ts_suffix = int(time.time()) % 1000000 只有秒级精度，同一秒内对同一需求
    # 重复提交（双击/多人协作）会生成相同 ID，第二次 commit 抛 IntegrityError 500，
    # 且无重试与幂等。项目已有 v3.1.3 因同类碰撞引入的 gen_test_case_id，手工路径未使用。
    from app.utils.id_gen import gen_test_case_id
    case_id = gen_test_case_id(module_prefix="MANUAL")

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

    # P1-1 产品隔离（v3.4.1 修复 M-9）：改为按需求归属校验。
    # 原实现用 limit(1) 抽查该需求下"任意一条"用例的 product_id，混合数据（迁移/修复产生）
    # 时会漏校验；且该需求下无用例时整段校验被跳过，后面却按 req_id 全量删除
    # （含 Traceability、ErrorCase 与 Milvus 向量）。此处改查 Requirement.product_id。
    req_row = await session.get(Requirement, req_id)
    if req_row is not None:
        await assert_product_visible(session, user, req_row.product_id)

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

    # v3.4.1 修复 M-10：请求中存在、但库里查不到的 ID 此前被静默忽略（不报错、也不计入
    # updated_count），前端误以为全部成功。现在显式回传 not_found 列表。
    found_ids = {r.id for r in records}
    not_found_ids = [cid for cid in dict.fromkeys(req.case_ids or []) if cid not in found_ids]

    updated_count = 0
    # v3.3（docs/14 二期 11）：批量反馈同样立即重算 weight（与单条端点保持一致）
    try:
        from app.utils.case_weight import calc_case_weight
        _recalc = True
    except Exception:  # noqa: BLE001
        _recalc = False

    for record in records:
        record.status = new_status
        record.feedback = feedback_data
        record.reviewed_by = user.username
        record.reviewed_at = datetime.now(timezone.utc)
        if _recalc:
            try:
                w, _detail = calc_case_weight(
                    record.feedback, record.confidence,
                    record.execution_count, record.last_pass_rate,
                )
                record.weight = w
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[API/testcases] 批量反馈重算 weight 失败 case_id={}: {}",
                    record.id, str(e)[:120],
                )
        updated_count += 1

    # v3.4（docs/15 §4.4）：向量投递意图与状态变更**同一事务**入出箱。
    # 原实现是在 commit 之后直接写 Milvus —— 中间崩溃就是"PG 已改、向量没改"，
    # 且没有任何地方记录这次缺失。现在改成：commit 后由 relay 至少一次投递
    # （approved → 幂等 upsert / rejected → delete），失败进重试与对账。
    # 入箱成功即算数（milvus_written 语义 = "已入出箱条数"，真正投递由 relay 完成）。
    from app.core.vector_outbox import enqueue as _enqueue_outbox

    milvus_written = 0
    milvus_failed = 0
    for record in records:
        try:
            if req.accepted:
                case_json = dict(record.case_json or {})
                case_json.setdefault("id", record.id)
                _enqueue_outbox(
                    session,
                    aggregate_id=record.id,
                    action="upsert",
                    revision=int(datetime.now(timezone.utc).timestamp() * 1000),
                    payload={
                        "case_json": case_json,
                        "metadata": {
                            "req_id": record.req_id,
                            "product_id": record.product_id,
                            "case_type": record.type,
                            "confidence": str(record.confidence),
                            "gen_batch_id": record.gen_batch_id or "",
                        },
                    },
                    created_by="api",
                )
            else:
                _enqueue_outbox(
                    session, aggregate_id=record.id, action="delete",
                    revision=int(datetime.now(timezone.utc).timestamp() * 1000),
                    payload={}, created_by="api",
                )
            milvus_written += 1
        except Exception as e:
            milvus_failed += 1
            logger.warning(
                "[API/testcases] 批量审批向量出箱入队失败: case_id={}, err={}",
                record.id, str(e)[:100],
            )

    await session.commit()

    logger.info(
        "[API/testcases] 批量审核: count={}, accepted={}, by={}, vector_enqueued={}, enqueue_failed={}",
        updated_count, req.accepted, user.username, milvus_written, milvus_failed,
    )

    return ApiResponse(
        success=True,
        message=(
            f"已批量{'通过' if req.accepted else '驳回'} {updated_count} 个用例"
            + (f"，另有 {len(not_found_ids)} 个用例不存在已跳过" if not_found_ids else "")
        ),
        data={
            "updated_count": updated_count,
            "not_found": not_found_ids,
            "not_found_count": len(not_found_ids),
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

    # v3.3（docs/14 二期 11）：人工反馈 → **立即重算该条 weight**。
    # 设计 §B3 的闭环要求"反馈影响下次检索排序"；若等周期任务重算，
    # 反馈与生效之间会隔一个周期。人工采纳率是 weight 的**最大权重项（0.35）**，
    # 立即生效收益最直接。
    try:
        from app.utils.case_weight import calc_case_weight

        w, _detail = calc_case_weight(
            record.feedback, record.confidence,
            record.execution_count, record.last_pass_rate,
        )
        record.weight = w
    except Exception as e:  # noqa: BLE001
        logger.warning("[API/testcases] 反馈后重算 weight 失败（不阻断）: {}", str(e)[:150])

    # v3.4（docs/15 §4.4）：向量投递意图与状态变更**同一事务**入出箱
    # （approved → 幂等 upsert / rejected → delete），commit 后由 relay 至少一次投递。
    # 与 batch_feedback 保持同一套语义；milvus_ok 现在表示"入箱是否成功"。
    from app.core.vector_outbox import enqueue as _enqueue_outbox

    milvus_ok = True
    try:
        if req.accepted:
            case_json = dict(record.case_json or {})
            case_json.setdefault("id", case_id)
            _enqueue_outbox(
                session,
                aggregate_id=case_id,
                action="upsert",
                revision=int(datetime.now(timezone.utc).timestamp() * 1000),
                payload={
                    "case_json": case_json,
                    "metadata": {
                        "req_id": record.req_id,
                        "product_id": record.product_id,
                        "case_type": record.type,
                        "confidence": str(record.confidence),
                        "gen_batch_id": record.gen_batch_id or "",
                    },
                },
                created_by="api",
            )
        else:
            _enqueue_outbox(
                session, aggregate_id=case_id, action="delete",
                revision=int(datetime.now(timezone.utc).timestamp() * 1000),
                payload={}, created_by="api",
            )
    except Exception as e:  # noqa: BLE001
        milvus_ok = False
        logger.warning(
            "[API/testcases] 单条审批向量出箱入队失败: case_id={}, err={}", case_id, str(e)[:100],
        )

    await session.commit()

    logger.info(
        "[API/testcases] 反馈已记录: case_id={}, accepted={}, status={}, outbox_ok={}",
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
