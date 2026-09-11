"""数据访问仓储层（Repositories）。

为编排层（core/orchestrator.py）提供统一的数据查询入口，
替代编排层直接 import async_session + ORM 模型的穿透式访问，
保证查询口径（字段形状）全流水线一致。

分层约定：core → db 单向依赖（合法方向）；
本模块不得 import agents / api / core 上层模块。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.database import async_session
from app.db.models import (
    AnalysisTask,
    FPDependency,
    FunctionalPoint,
    Project,
    Requirement,
    TestCase,
)


async def load_functional_points(req_id: str) -> list[dict]:
    """加载需求下全部功能点（完整形状，FP 统一形状的唯一权威定义）。

    返回形状：{id, name, fp_type, srr_json, atomicity_check}
    （v3.1.3 前 _gen_node 与 _test_design_node 加载形状不一致，现统一由本函数定义）

    Args:
        req_id: 需求 ID

    Returns:
        list[dict]: 功能点列表；无功能点或 req_id 为空时返回 []
    """
    if not req_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
        )
        return [
            {
                "id": fp.id,
                "name": fp.name,
                "fp_type": fp.fp_type,
                "srr_json": fp.srr_json or {},
                # v2.48: test_hints 已移除，不再传入 gen_agent
                "atomicity_check": fp.atomicity_check or {},
            }
            for fp in result.scalars().all()
        ]


async def load_fp_id_names(req_id: str) -> list[tuple[str, str]]:
    """加载需求下功能点的 (id, name) 列表（覆盖率矩阵用轻量查询）。

    Args:
        req_id: 需求 ID

    Returns:
        list[tuple[str, str]]: [(fp_id, fp_name), ...]
    """
    if not req_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(
                FunctionalPoint.requirement_id == req_id
            )
        )
        return [(fid, fname) for fid, fname in result.all()]


async def load_latest_completed_task(
    req_id: str, task_type: str = "generate_cases"
) -> dict[str, Any] | None:
    """加载需求最近一次已完成的分析任务 result_json（一致性检查历史基线）。

    Args:
        req_id: 需求 ID
        task_type: 任务类型（默认 generate_cases）

    Returns:
        dict | None: result_json（JSONB 已反序列化为 dict）；无记录返回 None
    """
    if not req_id:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(AnalysisTask.result_json)
            .where(
                AnalysisTask.req_id == req_id,
                AnalysisTask.task_type == task_type,
                AnalysisTask.status == "completed",
            )
            .order_by(AnalysisTask.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        # v2.0 修复：只取已完成任务，排除 running/failed，避免 prev_result 为空误判
        return row if isinstance(row, dict) else None


async def load_cases_by_req(req_id: str) -> list[dict]:
    """按需求 ID 检索全部测试用例（优化需求的原始用例检索）。

    Args:
        req_id: 需求 ID

    Returns:
        list[dict]: 每项 {id, title, type, case_json}
    """
    if not req_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(TestCase).where(TestCase.req_id == req_id)
        )
        return [
            {
                "id": r.id,
                "title": r.title,
                "type": r.type,
                "case_json": r.case_json,
            }
            for r in result.scalars().all()
        ]


# ============================================================================
# v3.2.5: 高频查询模式收敛（api/agents 裸 select() 收敛到仓储层）
# ============================================================================

async def get_requirement_by_id(req_id: str):
    """按 ID 获取需求 ORM 对象（最高频查询模式）。"""
    if not req_id:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(Requirement).where(Requirement.id == req_id)
        )
        return result.scalar_one_or_none()


async def get_project_by_id(project_id: str):
    """按 ID 获取项目 ORM 对象。"""
    if not project_id:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(Project).where(Project.id == project_id)
        )
        return result.scalar_one_or_none()


async def list_fps_by_requirement_id(requirement_id: str):
    """按需求 ID 获取全部功能点 ORM 对象。"""
    if not requirement_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(FunctionalPoint).where(
                FunctionalPoint.requirement_id == requirement_id
            )
        )
        return list(result.scalars().all())


async def list_fp_dependencies_by_req(req_id: str):
    """按需求 ID 获取全部 FP 依赖边。"""
    if not req_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(FPDependency).where(FPDependency.requirement_id == req_id)
        )
        return list(result.scalars().all())


async def list_cases_by_req_id(req_id: str):
    """按需求 ID 获取全部测试用例 ORM 对象（含所有字段）。"""
    if not req_id:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(TestCase).where(TestCase.req_id == req_id)
        )
        return list(result.scalars().all())
