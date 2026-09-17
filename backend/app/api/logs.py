"""日志管理 API — 管理员可查询 LLM 调用日志和用户操作日志。

端点清单：
    GET /api/admin/llm-logs          LLM 调用日志列表（分页，不含完整 prompt/response）
    GET /api/admin/llm-logs/{id}     LLM 调用日志详情（含完整 prompt/response）
    GET /api/admin/operation-logs    用户操作日志列表（分页）

所有端点均需 admin 权限。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as _tz

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin
from app.utils.pagination import paginate
from app.db.database import get_session
from app.db.models import LLMCallLog, OperationLog
from app.models.api_schemas import ApiResponse

router = APIRouter()


# v3.4.1 修复 M-5：日期筛选按**业务时区**（Asia/Shanghai）解释"当天"，再换算成 UTC 边界。
# 原实现硬编码 .replace(tzinfo=utc)：中国用户筛选"当天 D"时得到的是 UTC D 00:00~23:59，
# 即本地 D 08:00 ~ D+1 08:00 —— 既漏掉本地 00:00~08:00 的日志（UTC D-1 16:00~24:00），
# 又误含次日凌晨的日志。
try:
    from zoneinfo import ZoneInfo

    _BIZ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 时区库不可用时退化为 UTC（行为与修复前一致）
    _BIZ_TZ = _tz.utc


def _parse_date_start(value: str) -> datetime | None:
    """将 'YYYY-MM-DD' 转为业务时区当日 00:00:00 对应的 UTC datetime。无效返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_BIZ_TZ)
        return dt.astimezone(_tz.utc)
    except ValueError:
        return None


def _parse_date_end(value: str) -> datetime | None:
    """将 'YYYY-MM-DD' 转为业务时区当日 23:59:59 对应的 UTC datetime。无效返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=_BIZ_TZ
        )
        return dt.astimezone(_tz.utc)
    except ValueError:
        return None


# ============================================================================
# LLM 调用日志
# ============================================================================

@router.get("/llm-logs", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_llm_logs(
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数（最大 100）"),
    agent_name: str | None = Query(default=None, description="按 Agent 名称筛选"),
    model: str | None = Query(default=None, description="按模型名称筛选（模糊匹配）"),
    task_id: str | None = Query(default=None, description="按任务 ID 筛选"),
    req_id: str | None = Query(default=None, description="按需求 ID 筛选"),
    success: bool | None = Query(default=None, description="按成功/失败筛选"),
    start_date: str | None = Query(default=None, description="开始日期（YYYY-MM-DD，含当天）"),
    end_date: str | None = Query(default=None, description="结束日期（YYYY-MM-DD，含当天）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查询 LLM 调用日志列表（分页）。

    列表仅返回摘要字段（不含完整 prompt/response），详情请用 GET /admin/llm-logs/{id}。
    """
    # 构建查询条件
    conditions = []
    if agent_name:
        conditions.append(LLMCallLog.agent_name == agent_name)
    if model:
        conditions.append(LLMCallLog.model.ilike(f"%{model}%"))
    if task_id:
        conditions.append(LLMCallLog.task_id == task_id)
    if req_id:
        conditions.append(LLMCallLog.req_id == req_id)
    if success is not None:
        conditions.append(LLMCallLog.success == success)
    dt_start = _parse_date_start(start_date) if start_date else None
    dt_end = _parse_date_end(end_date) if end_date else None
    if dt_start:
        conditions.append(LLMCallLog.created_at >= dt_start)
    if dt_end:
        conditions.append(LLMCallLog.created_at <= dt_end)

    # 分页查询（按创建时间倒序）—— v3.2.2 收敛
    stmt = select(LLMCallLog).order_by(LLMCallLog.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    logs, total = await paginate(session, stmt, page, page_size)

    items = [
        {
            "id": log.id,
            "agent_name": log.agent_name,
            "task_id": log.task_id,
            "req_id": log.req_id,
            "provider": log.provider,
            "model": log.model,
            "temperature": log.temperature,
            "duration_ms": log.duration_ms,
            "token_usage": log.token_usage,
            "success": log.success,
            "error_message": log.error_message,
            "created_at": log.created_at.isoformat() if log.created_at else "",
        }
        for log in logs
    ]

    return ApiResponse(
        success=True,
        message=f"找到 {total} 条 LLM 调用日志",
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("/llm-logs/{log_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def get_llm_log(
    log_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查询单条 LLM 调用日志详情（含完整 prompt/response）。"""
    log = await session.get(LLMCallLog, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail=f"LLM 调用日志 {log_id} 不存在")

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "id": log.id,
            "agent_name": log.agent_name,
            "task_id": log.task_id,
            "req_id": log.req_id,
            "provider": log.provider,
            "model": log.model,
            "system_prompt": log.system_prompt,
            "user_prompt": log.user_prompt,
            "response": log.response,
            "parsed_json": log.parsed_json,
            "temperature": log.temperature,
            "duration_ms": log.duration_ms,
            "token_usage": log.token_usage,
            "success": log.success,
            "error_message": log.error_message,
            "created_at": log.created_at.isoformat() if log.created_at else "",
        },
    )


# ============================================================================
# 用户操作日志
# ============================================================================

@router.get("/operation-logs", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_operation_logs(
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数（最大 100）"),
    username: str | None = Query(default=None, description="按用户名筛选"),
    action: str | None = Query(default=None, description="按操作类型筛选"),
    target_type: str | None = Query(default=None, description="按目标类型筛选"),
    target_id: str | None = Query(default=None, description="按目标 ID 筛选"),
    success: bool | None = Query(default=None, description="按成功/失败筛选"),
    start_date: str | None = Query(default=None, description="开始日期（YYYY-MM-DD，含当天）"),
    end_date: str | None = Query(default=None, description="结束日期（YYYY-MM-DD，含当天）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查询用户操作日志列表（分页）。"""
    # 构建查询条件
    conditions = []
    if username:
        conditions.append(OperationLog.username == username)
    if action:
        conditions.append(OperationLog.action == action)
    if target_type:
        conditions.append(OperationLog.target_type == target_type)
    if target_id:
        conditions.append(OperationLog.target_id == target_id)
    if success is not None:
        conditions.append(OperationLog.success == success)
    dt_start = _parse_date_start(start_date) if start_date else None
    dt_end = _parse_date_end(end_date) if end_date else None
    if dt_start:
        conditions.append(OperationLog.created_at >= dt_start)
    if dt_end:
        conditions.append(OperationLog.created_at <= dt_end)

    # 分页查询（按创建时间倒序）—— v3.2.2 收敛
    stmt = select(OperationLog).order_by(OperationLog.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    logs, total = await paginate(session, stmt, page, page_size)

    items = [
        {
            "id": log.id,
            "user_id": log.user_id,
            "username": log.username,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "detail": log.detail,
            "ip_address": log.ip_address,
            "success": log.success,
            "error_message": log.error_message,
            "created_at": log.created_at.isoformat() if log.created_at else "",
        }
        for log in logs
    ]

    return ApiResponse(
        success=True,
        message=f"找到 {total} 条操作日志",
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )
