# -*- coding: utf-8 -*-
"""可观测性（P3-6，依据《深入理解 AI Agent》6.8）。

Trace = 一次生成/分析任务；Span = 任务内的每个 LLM 调用 / 节点阶段。
数据基础已存在：analysis_tasks（任务级）+ llm_call_logs（LLM 调用级，含
agent_name/req_id/duration/token_usage/success）——本模块把它们串成 span 树。

失败轨迹回流（reflux）：书 6.8 最有价值的闭环——把生产失败案例沉淀为
评估集新用例，使评估集成"活资产"。见 scripts/reflux_failures.py。
"""
from __future__ import annotations

from sqlalchemy import select
from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user, require_write
from app.models.api_schemas import ApiResponse
from app.db.database import get_session
from app.db.models import AnalysisTask, LLMCallLog
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["可观测性"])


# ============================================================================
# 向量出箱积压（v3.4 / docs/15 §4.4 的运维面：PG↔Milvus 一致性是否健康，一眼可见）
# ============================================================================

@router.get("/vector-outbox", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def vector_outbox_status(session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """出箱积压概览 + 最近待投递/失败事件（运维巡检）。

    pending 长期为 0 = 正常；持续增长看 api 日志 [VectorOutbox]；
    failed 是重试超上限的，需要人工看 error 并跑 reconcile_cases。
    """
    from app.core.vector_outbox import pending_summary

    summary = await pending_summary()
    recent = (await session.execute(_text("""
        SELECT event_id, aggregate_id, action, status, retry_count, error, next_retry_at, created_at
        FROM vector_outbox
        WHERE status IN ('pending', 'failed')
        ORDER BY created_at DESC
        LIMIT 20
    """))).mappings().all()
    return ApiResponse(success=True, message="ok", data={
        "summary": summary,
        "recent": [dict(r) for r in recent],
    })


@router.post("/vector-outbox/dispatch", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def vector_outbox_dispatch() -> ApiResponse:
    """手动触发一轮投递（relay 本身 5s 自动跑，这是给运维的立即重试入口）。"""
    from app.core.vector_outbox import dispatch_batch

    stats = await dispatch_batch()
    return ApiResponse(success=True, message="ok", data=stats)


@router.get("/trace/{task_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_trace(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """按任务聚合 span 树：任务根 span + 各 Agent 的 LLM 调用子 span。

    用途：定位"分数下降先查评测系统还是 Agent"（书 6.8）——
    看到异常指标时，先审完整轨迹区分"任务真的失败"与"评测系统本身故障"。
    """
    task = (await session.execute(
        select(AnalysisTask).where(AnalysisTask.id == task_id)
    )).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    # v3.4（docs/15 §4.10）：trace 收紧为**严格 task_id**。
    # 旧实现 `task_id OR req_id` 会把同一需求的**其它运行**的调用混进来，
    # trace 就不再是"这一次运行"的轨迹。trace_id = task_id（每次运行一个 trace）。
    logs = (await session.execute(
        select(LLMCallLog)
        .where(LLMCallLog.task_id == task_id)
        .order_by(LLMCallLog.created_at.asc())
    )).scalars().all()

    # 按 Agent 聚合为子 span 组
    agents: dict[str, dict] = {}
    for log in logs:
        entry = agents.setdefault(log.agent_name, {
            "agent": log.agent_name,
            "calls": [],
            "total_duration_ms": 0,
            "success": 0,
            "failed": 0,
            "cache_hit_tokens": 0,
        })
        usage = log.token_usage if isinstance(log.token_usage, dict) else {}
        entry["calls"].append({
            "provider": log.provider,
            "model": log.model,
            "duration_ms": log.duration_ms,
            "token_usage": usage,
            "success": log.success,
            "error": (log.error_message or "")[:200],
            "created_at": str(log.created_at) if log.created_at else None,
        })
        entry["total_duration_ms"] += log.duration_ms or 0
        entry["success" if log.success else "failed"] += 1
        entry["cache_hit_tokens"] += int(usage.get("cache_hit_tokens") or 0)

    result_json = task.result_json if isinstance(task.result_json, dict) else {}
    return ApiResponse(success=True, message="ok", data={
        "task": {
            "id": task.id,
            "req_id": task.req_id,
            "phase": task.phase,
            "status": task.status,
            "progress": task.progress,
            "message": task.message,
            "phase_breakdown": result_json.get("phase_breakdown") or {},
        },
        "agents": list(agents.values()),
        "llm_call_count": len(logs),
    })
