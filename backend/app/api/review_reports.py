"""评审报告中心路由（v3.1.3 P1/P2/P3）。

提供评审报告的列表查询与详情查看，覆盖两类报告：
- report_type="srr"：需求分析阶段 QualityGate 质量门禁报告
- report_type="case"：用例生成阶段 ReviewAgent 6 维度审核报告

端点清单：
    GET  /api/review-reports                          报告列表（产品隔离 + 类型/需求筛选 + 分页）
    GET  /api/review-reports/stats                    统计
    POST /api/review-reports/{id}/issues/adopt        P2 批量采纳/驳回意见
    GET  /api/review-reports/prompt-evolution/targets P3 可整合的目标提示词列表
    POST /api/review-reports/prompt-evolution         P3 执行多报告意见整合 → 提示词新版本
    GET  /api/review-reports/prompt-evolution         P3 整合历史列表
    GET  /api/review-reports/{id}                     报告详情（含维度置信度、问题列表、原始报告）

路由顺序注意：/stats、/prompt-evolution/* 等固定路径必须注册在 /{report_id} 之前，
否则会被路径参数捕获。

设计依据：
- v3.1.3 评审报告中心 P1（多报告聚合优化提示词的基座）/ P2（意见采纳）/ P3（整合进化）
- 产品隔离硬约束：必须按 product_id 过滤
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user, require_write
from app.core.product_access import (
    apply_visible_product_filter,
    assert_product_visible,
    get_visible_product_ids,
)
from app.utils.pagination import paginate
from app.db import prompt_store
from app.core import prompt_manager
from app.core.prompt_evolution import evolve_prompt
from app.db.database import get_session
from app.db.models import PromptIntegrationRecord, ReviewReportRecord
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

router = APIRouter()


@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_review_reports(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    report_type: str | None = Query(default=None, description="按类型筛选 srr/case"),
    req_id: str | None = Query(default=None, description="按需求 ID 筛选"),
    task_id: str | None = Query(default=None, description="按任务 ID 筛选"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """获取评审报告列表（按创建时间倒序）。

    筛选条件：product_id / report_type / req_id / task_id
    列表项含维度置信度摘要（前端可直接渲染维度分条），详情字段（issues/summary）
    走详情端点。
    """
    # v4.1.9：原实现只按用户传入的 product_id 过滤，不传即返回全库报告
    if product_id:
        await assert_product_visible(session, user, product_id)
    visible_ids = await get_visible_product_ids(session, user)

    conditions = []
    if product_id:
        conditions.append(ReviewReportRecord.product_id == product_id)
    if report_type:
        conditions.append(ReviewReportRecord.report_type == report_type)
    if req_id:
        conditions.append(ReviewReportRecord.req_id == req_id)
    if task_id:
        conditions.append(ReviewReportRecord.task_id == task_id)
    apply_visible_product_filter(conditions, visible_ids, ReviewReportRecord.product_id)

    # v3.2.2 收敛：count+分页统一走 utils.pagination
    stmt = select(ReviewReportRecord).order_by(ReviewReportRecord.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    rows, total = await paginate(session, stmt, page, page_size)

    items = [
        {
            "id": r.id,
            "report_type": r.report_type,
            "req_id": r.req_id,
            "req_title": r.req_title,
            "product_id": r.product_id,
            "task_id": r.task_id,
            "gen_batch_id": r.gen_batch_id,
            "overall_confidence": r.overall_confidence,
            "passed": r.passed,
            "issue_count": r.issue_count,
            # P3：已采纳意见数（整合抽屉据此展示可消费的报告）
            "accepted_count": sum(
                1 for i in (r.issues or [])
                if isinstance(i, dict) and i.get("adoption_status") == "accepted"
            ),
            "dimension_scores": r.dimension_scores or {},
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]

    return ApiResponse(
        success=True,
        message="ok",
        data={"items": items, "total": total, "page": page, "page_size": page_size},
    )


@router.get("/stats", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def review_report_stats(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """评审报告统计（按类型汇总，供报告中心顶部概览卡片）。"""
    # v4.1.9：query 参数需显式校验 + 按可见集合过滤
    if product_id:
        await assert_product_visible(session, user, product_id)
    visible_ids = await get_visible_product_ids(session, user)

    stmt = (
        select(
            ReviewReportRecord.report_type,
            func.count(ReviewReportRecord.id),
            func.avg(ReviewReportRecord.overall_confidence),
        )
        .group_by(ReviewReportRecord.report_type)
    )
    stat_conditions: list = []
    if product_id:
        stat_conditions.append(ReviewReportRecord.product_id == product_id)
    apply_visible_product_filter(stat_conditions, visible_ids, ReviewReportRecord.product_id)
    if stat_conditions:
        stmt = stmt.where(*stat_conditions)
    rows = (await session.execute(stmt)).all()

    stats = {
        rtype: {"count": cnt, "avg_confidence": round(float(avg or 0.0), 3)}
        for rtype, cnt, avg in rows
    }
    return ApiResponse(success=True, message="ok", data=stats)


# ============================================================================
# P3：多报告意见整合 → 提示词新版本（v3.1.3）
# ============================================================================

# 整合目标白名单：只允许整合到这些提示词（避免误操作知识问答等无关 prompt）
EVOLUTION_TARGETS: list[dict] = [
    {
        "prompt_id": "gen_agent.SYSTEM_PROMPT",
        "module": "gen_agent",
        "name": "SYSTEM_PROMPT",
        "label": "用例生成（GenAgent）",
        "description": "用例评审意见的主要整合目标 — 修正生成行为、避免重复缺陷",
        "report_types": ["case"],
    },
    {
        "prompt_id": "review_agent.SYSTEM_PROMPT",
        "module": "review_agent",
        "name": "SYSTEM_PROMPT",
        "label": "用例审核（ReviewAgent）",
        "description": "审核规则本身的系统性遗漏 — 调整审核维度与判定标准",
        "report_types": ["case"],
    },
    {
        "prompt_id": "req_agent.SYSTEM_PROMPT",
        "module": "req_agent",
        "name": "SYSTEM_PROMPT",
        "label": "需求解析（ReqAgent）",
        "description": "需求评审意见的整合目标 — 改进 SRR 解析质量",
        "report_types": ["srr"],
    },
    {
        "prompt_id": "fp_agent.FP_INTEGRATED_SYSTEM_PROMPT",
        "module": "fp_agent",
        "name": "FP_INTEGRATED_SYSTEM_PROMPT",
        "label": "功能点拆分（FP-Agent）",
        "description": "需求评审中拆分相关意见的整合目标",
        "report_types": ["srr"],
    },
]


@router.get(
    "/prompt-evolution/targets",
    response_model=ApiResponse,
    dependencies=[Depends(get_current_user)],
)
async def list_evolution_targets() -> ApiResponse:
    """P3 可整合的目标提示词列表（白名单 + 各自历史版本数）。"""
    items = []
    for t in EVOLUTION_TARGETS:
        try:
            history = prompt_manager.list_history(t["module"], t["name"], limit=500)
            history_count = len(history)
        except Exception:
            history_count = 0
        items.append({**t, "history_count": history_count})
    return ApiResponse(success=True, message="ok", data={"items": items})


class PromptCandidateActivateRequest(BaseModel):
    """候选激活请求（P0-5）。

    `evaluation_run_id` 是**唯一**放行依据：必须是服务器端记录、status=passed、
    且 prompt_hash 与被激活候选文本逐字一致的评估运行。
    `confirmed_eval` 仅为兼容旧调用方保留，**不再参与判定**（原先只信客户端自证）。
    """
    evaluation_run_id: str = Field("", max_length=64, description="服务器端评估运行 ID（必需）")
    confirmed_eval: bool = Field(False, description="[已废弃] 不再作为放行依据")
    eval_notes: str = Field("", max_length=500, description="回归结果备注")


class PromptEvolutionRequest(BaseModel):
    """P3 整合请求。"""
    product_id: str = Field(..., description="产品隔离 ID")
    report_ids: list[str] = Field(..., min_length=1, description="参与整合的评审报告 ID 列表")
    target_prompt_id: str = Field(..., description="目标提示词 ID（module.name，须在白名单内）")
    manual_notes: str = Field("", description="人工补充意见（最高优先级）")
    # P2-5: 默认 False——产物先进候选区（status=pending），回归确认后经 activate 端点激活
    activate: bool = Field(False, description="是否直接激活（True=旧行为直写 override；False=进候选区）")


@router.post(
    "/prompt-evolution",
    response_model=ApiResponse,
    dependencies=[Depends(require_write)],
)
async def run_prompt_evolution(
    req: PromptEvolutionRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """P3 主入口：多报告已采纳意见 + 人工意见 → LLM 整合 → 提示词新版本。

    增量补丁式设计：prompt 主体不动，只更新末尾"历史评审沉淀规则"区段。
    写入自动产生 prompt_history 记录，返回 prompt_history_id 供回退
    （POST /api/admin/prompts/{module}/{name}/restore/{history_id}）。

    Raises:
        HTTPException 400: 无已采纳意见 / 目标不在白名单 / LLM 整合失败
    """
    target = next(
        (t for t in EVOLUTION_TARGETS if t["prompt_id"] == req.target_prompt_id), None
    )
    if target is None:
        raise HTTPException(status_code=400, detail=f"目标提示词 {req.target_prompt_id} 不在可整合白名单内")

    try:
        result = await evolve_prompt(
            session=session,
            product_id=req.product_id,
            report_ids=req.report_ids,
            target_module=target["module"],
            target_name=target["name"],
            manual_notes=req.manual_notes,
            username=user.username,
            activate=req.activate,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])

    return ApiResponse(
        success=True,
        message=f"整合完成：{result['accepted_count']} 条已采纳意见已沉淀至 {result['target_prompt_id']}",
        data=result,
    )


@router.get(
    "/prompt-evolution",
    response_model=ApiResponse,
    dependencies=[Depends(get_current_user)],
)
async def list_prompt_evolution_history(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    target_prompt_id: str | None = Query(default=None, description="按目标提示词筛选"),
    limit: int = Query(default=20, ge=1, le=100, description="返回条数上限"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """P3 整合历史列表（含回退锚点 prompt_history_id）。"""
    stmt = select(PromptIntegrationRecord).order_by(
        PromptIntegrationRecord.created_at.desc()
    ).limit(limit)
    if product_id:
        stmt = stmt.where(PromptIntegrationRecord.product_id == product_id)
    if target_prompt_id:
        stmt = stmt.where(PromptIntegrationRecord.target_prompt_id == target_prompt_id)
    rows = (await session.execute(stmt)).scalars().all()

    items = [
        {
            "id": r.id,
            "product_id": r.product_id,
            "report_ids": r.report_ids or [],
            "report_types": r.report_types or [],
            "target_prompt_id": r.target_prompt_id,
            "accepted_count": r.accepted_count,
            "manual_notes": r.manual_notes,
            "integration_summary": r.integration_summary,
            "prompt_history_id": r.prompt_history_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]
    return ApiResponse(success=True, message="ok", data={"items": items, "total": len(items)})


@router.get("/{report_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_review_report(
    report_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """获取评审报告详情（含维度置信度、问题列表、原始报告全文）。"""
    record = await session.get(ReviewReportRecord, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"评审报告 {report_id} 不存在")

    # v4.1.9：报告含 issues/summary 全文，需校验归属产品
    await assert_product_visible(session, user, record.product_id)

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "id": record.id,
            "report_type": record.report_type,
            "req_id": record.req_id,
            "req_title": record.req_title,
            "product_id": record.product_id,
            "task_id": record.task_id,
            "gen_batch_id": record.gen_batch_id,
            "overall_confidence": record.overall_confidence,
            "passed": record.passed,
            "issue_count": record.issue_count,
            "dimension_scores": record.dimension_scores or {},
            "issues": record.issues or [],
            "summary": record.summary or {},
            "created_by": record.created_by,
            "created_at": record.created_at.isoformat() if record.created_at else "",
        },
    )


# ============================================================================
# P2：报告意见采纳（v3.1.3）
# ============================================================================

class IssueAdoptionItem(BaseModel):
    """单条采纳操作。"""
    issue_id: str = Field(..., description="问题 ID（报告内唯一，如 RR-xxx-000）")
    accepted: bool = Field(..., description="True=采纳 / False=驳回")
    comment: str = Field("", description="人工补充意见（采纳理由/驳回原因）")


class IssueAdoptionRequest(BaseModel):
    """批量采纳请求。"""
    items: list[IssueAdoptionItem] = Field(..., min_length=1, description="采纳操作列表")


@router.post(
    "/{report_id}/issues/adopt",
    response_model=ApiResponse,
    dependencies=[Depends(require_write)],
)
async def adopt_issues(
    report_id: str,
    req: IssueAdoptionRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """批量采纳/驳回报告中的评审意见（v3.1.3 P2）。

    对报告 issues 列表中指定 issue_id 的意见执行采纳/驳回，支持人工补充意见。
    采纳状态写入 issue 的 adoption_status / adoption_comment / adopted_by / adopted_at，
    供 P3 多报告聚合整合（只消费 accepted 意见）。

    Args:
        report_id: 报告 ID
        req: 采纳操作列表（issue_id + accepted + comment）
        user: 当前用户
        session: 数据库会话

    Returns:
        ApiResponse: data 含 updated / not_found / adoption_summary（供 P3 消费的统计）

    Raises:
        HTTPException 404: 报告不存在
    """
    record = await session.get(ReviewReportRecord, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"评审报告 {report_id} 不存在")

    # v4.1.9：原实现仅 require_write，任意写权限用户可跨产品采纳/驳回意见
    await assert_product_visible(session, user, record.product_id)

    issues = record.issues or []
    issue_map = {i.get("issue_id", ""): i for i in issues if isinstance(i, dict)}
    if not issue_map:
        raise HTTPException(status_code=400, detail="该报告无可采纳的评审意见")

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    not_found: list[str] = []

    for item in req.items:
        issue = issue_map.get(item.issue_id)
        if issue is None:
            not_found.append(item.issue_id)
            continue
        issue["adoption_status"] = "accepted" if item.accepted else "rejected"
        issue["adoption_comment"] = (item.comment or "").strip()
        issue["adopted_by"] = user.username
        issue["adopted_at"] = now
        updated += 1

    if updated == 0:
        await session.commit()
        return ApiResponse(
            success=False,
            message=f"未找到任何有效意见: {not_found}",
            data={"updated": 0, "not_found": not_found},
        )

    record.issues = issues
    # SQLAlchemy JSON 列陷阱：issues 列表是同一对象，原位修改后即使重新赋值，
    # 新旧值 == 相等也不会产生 UPDATE。必须 flag_modified 显式标记变更。
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(record, "issues")
    await session.commit()

    # 采纳统计（供 P3 聚合整合消费）
    accepted = sum(1 for i in issues if i.get("adoption_status") == "accepted")
    rejected = sum(1 for i in issues if i.get("adoption_status") == "rejected")
    pending = sum(1 for i in issues if i.get("adoption_status", "pending") == "pending")

    logger.info(
        "[API/review-reports] 意见采纳: report={}, updated={}, by={}, accepted={}/rejected={}/pending={}",
        report_id, updated, user.username, accepted, rejected, pending,
    )

    return ApiResponse(
        success=True,
        message=f"已处理 {updated} 条意见" + (f"，{len(not_found)} 条未找到" if not_found else ""),
        data={
            "updated": updated,
            "not_found": not_found,
            "adoption_summary": {
                "total": len(issues),
                "accepted": accepted,
                "rejected": rejected,
                "pending": pending,
            },
        },
    )


# ============================================================================
# P2-5: 提示词候选区激活（书 6.10.4/8.3——回归确认后才落 override）
# ============================================================================

@router.get("/prompt-evolution/candidates", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_candidates(
    status: str = "pending",
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出提示词进化候选（默认 pending）。"""
    rows = (await session.execute(
        select(PromptIntegrationRecord).where(PromptIntegrationRecord.status == status)
        .order_by(PromptIntegrationRecord.created_at.desc())
    )).scalars().all()
    return ApiResponse(success=True, message="ok", data=[{
        "id": r.id, "target_prompt_id": r.target_prompt_id,
        "accepted_count": r.accepted_count, "summary": r.integration_summary[:200],
        "status": r.status, "created_by": r.created_by,
    } for r in rows])


@router.post("/prompt-evolution/{record_id}/activate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def activate_candidate(
    record_id: str,
    req: PromptCandidateActivateRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """激活候选提示词（回归门槛）。

    v3.4（docs/15 §4.5 / P0-5）：门槛从"客户端声明 confirmed_eval=true"改为
    **服务器端评估证据**——必须提供 evaluation_run_id，且该运行满足：
    status=passed、target 与候选目标一致、有数据集 hash、口径版本一致、
    prompt_hash 与 candidate_prompt 逐字一致（防旧评估顶替新候选）。
    激活写入自动产生 prompt_history（回退锚点不变）。
    """
    from app.core.evaluation import (
        find_evaluation_run,
        mark_evaluation_run_consumed,
        validate_prompt_activation,
    )

    record = (await session.execute(
        select(PromptIntegrationRecord).where(PromptIntegrationRecord.id == record_id)
    )).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"整合记录不存在: {record_id}")
    if record.status != "pending":
        raise HTTPException(status_code=400, detail=f"记录状态为 {record.status}，仅 pending 可激活")
    if not record.candidate_prompt:
        raise HTTPException(status_code=400, detail="候选内容为空，无法激活")
    if not req.evaluation_run_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "回归门槛未满足：必须提供 evaluation_run_id（服务器端评估运行）。"
                "先执行 scripts/eval_prompt_candidate.py --integration-id "
                f"{record_id} 生成评估，再用返回的 run_id 激活；"
                "客户端 confirmed_eval 声明不再作为依据。"
            ),
        )
    run = await find_evaluation_run(req.evaluation_run_id)
    ok, reason = validate_prompt_activation(
        run, target=record.target_prompt_id, candidate_prompt=record.candidate_prompt,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    module, name = record.target_prompt_id.split(".", 1)
    prompt_store.update_prompt(module, name, record.candidate_prompt, changed_by=f"candidate:{user.username}")
    history = prompt_store.list_prompt_history(module, name, limit=1)
    record.status = "activated"
    record.activated_at = datetime.now(timezone.utc)
    record.activated_by = user.username
    record.prompt_history_id = history[0]["id"] if history else record.prompt_history_id
    await session.commit()
    await mark_evaluation_run_consumed(req.evaluation_run_id, record.id)
    return ApiResponse(success=True, message="候选提示词已激活（可经 prompt_history 回退）", data={
        "record_id": record.id, "target_prompt_id": record.target_prompt_id,
        "prompt_history_id": record.prompt_history_id,
        "evaluation_run_id": req.evaluation_run_id,
        "evaluation_score": (run or {}).get("score"),
    })
