# -*- coding: utf-8 -*-
"""可观测性（P3-6，依据《深入理解 AI Agent》6.8）。

Trace = 一次生成/分析任务；Span = 任务内的每个 LLM 调用 / 节点阶段。
数据基础已存在：analysis_tasks（任务级）+ llm_call_logs（LLM 调用级，含
agent_name/req_id/duration/token_usage/success）——本模块把它们串成 span 树。

失败轨迹回流（reflux）：书 6.8 最有价值的闭环——把生产失败案例沉淀为
评估集新用例，使评估集成"活资产"。见 scripts/reflux_failures.py。
"""
from __future__ import annotations

from sqlalchemy import select, or_
from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.models.api_schemas import ApiResponse
from app.db.database import get_session
from app.db.models import AnalysisTask, LLMCallLog
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["可观测性"])


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

    logs = (await session.execute(
        select(LLMCallLog)
        .where(or_(LLMCallLog.task_id == task_id, LLMCallLog.req_id == task.req_id))
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
