# -*- coding: utf-8 -*-
"""功能点最新版用例集聚合服务。

对称复用 fp_crsr.py 聚合模式：
  证据层（L0/L1）：各版本 test_cases 原样保留（case_id/case_json），可回溯
  场景层（L2）  ：functional_point_cases 表，项目归档时固化重建

聚合规则（用户确认，按 go_live_date 选最新版）：
  - 同名 FP（同一 product_id 下） → 取出所有 approved 用例
  - 关联用例所属需求 → 需求所属项目 → 按项目 go_live_date 降序排序，取上线时间最新的项目
  - 同一项目内，所有 approved 用例全部保留 → 按用例 updated_at 排 → 聚合后 content 就是该项目版本下这 FP 的全部 approved 用例
  - 也就是说："最新版" = "最新上线项目版本下全部 approved 用例"，因为用户说"版本号低也可能后上线"，所以必须按 go_live_date。

消费方：
  - 测试设计阶段（test_design），受影响 FP 直接拉本表 latest 用例 → 复用 + 缺口补生成（回归测试选择 RTS）
  - XMind 导出 → "回归测试"分类独立分组，和 functional/... 并列
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import (
    FunctionalPoint,
    FunctionalPointCases,
    Project,
    Requirement,
    TestCase,
)
from app.utils.logger import logger


def _updated_at_key(updated_at) -> float:
    if updated_at is None:
        return float("-inf")
    if isinstance(updated_at, datetime):
        try:
            return updated_at.timestamp()
        except (OverflowError, OSError, ValueError):
            return float("-inf")
    try:
        return datetime.fromisoformat(str(updated_at)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return float("-inf")


def _go_live_key(go_live) -> float:
    """上线时间排序键：值越大 = 上线越新。max(key) 选出最新上线的项目。

    防御：receive 可能是 naive/datetime/字符串，统一规约到 epoch 秒。
    """
    if go_live is None:
        return -1.0  # 未设置视为最早
    if isinstance(go_live, datetime):
        return go_live.timestamp()
    try:
        return datetime.fromisoformat(str(go_live)).timestamp()
    except Exception:
        return -1.0


def _case_matches_fp(case_json: dict, fp_name: str, fp_id: str | None = None) -> bool:
    if not isinstance(case_json, dict):
        return False
    if fp_id:
        case_fp_id = case_json.get("related_fp_id") or case_json.get("fp_id")
        if case_fp_id:
            return str(case_fp_id) == str(fp_id)
    related_fp = case_json.get("related_fp", "") or case_json.get("functional_point", "")
    return related_fp == fp_name


async def aggregate_latest_cases(
    session,
    product_id: str,
    fp_name: str,
    fp_id: str | None = None,
) -> dict:
    """实时聚合产品内同名功能点的最新版 approved 用例（不入库）。

    按用户要求：判定最新 = go_live_date（项目上线时间）最大的项目，该项目下所有
    approved 且 related_fp == fp_name 的用例就是最新基线。

    Returns:
        dict: {
            cases: 聚合后的用例列表（完整 case_json）,
            latest_go_live_date: 命中项目的 go_live_date（iso）,
            project_version: 命中项目的版本号,
            source_case_ids: 参与聚合的 case_id 列表,
            case_count: int,
            version_refs: 版本追溯[{project_id, version, go_live_date, case_count}],
        }
    """
    # Join 链路：TestCase → Requirement → Project
    # 过滤：TestCase.status == 'approved' AND related_fp == fp_name
    rows = (
        await session.execute(
            select(
                TestCase.id,
                TestCase.case_json,
                TestCase.updated_at,
                Requirement.id.label("req_id"),
                Requirement.title.label("req_title"),
                Project.id.label("proj_id"),
                Project.version.label("proj_version"),
                Project.go_live_date,
            )
            .join(Requirement, Requirement.id == TestCase.req_id)
            .join(Project, Project.id == Requirement.project_id)
            .where(
                Requirement.product_id == product_id,
                TestCase.status == "approved",
            )
        )
    ).all()

    version_refs = []
    cases_by_proj: dict[str, list] = {}  # proj_id → [case_json]
    proj_meta: dict[str, tuple] = {}  # proj_id → (version, go_live_date)

    for (
        case_id,
        case_json,
        updated_at,
        req_id,
        req_title,
        proj_id,
        proj_version,
        go_live_date,
    ) in rows:
        if not _case_matches_fp(case_json, fp_name, fp_id):
            continue
        if proj_id not in proj_meta:
            proj_meta[proj_id] = (proj_version, go_live_date)
        cases_by_proj.setdefault(proj_id, []).append(
            (case_json, updated_at, case_id)
        )
        version_refs.append({
            "proj_id": proj_id,
            "version": proj_version,
            "go_live_date": go_live_date.isoformat() if go_live_date else "",
            "case_id": case_id,
            "req_id": req_id,
            "req_title": req_title,
        })

    if not cases_by_proj:
        return {
            "cases": [],
            "latest_go_live_date": None,
            "project_version": "",
            "source_case_ids": [],
            "case_count": 0,
            "version_refs": [],
        }

    # 找 go_live_date 最大的项目（最近上线）
    best_proj_id = max(cases_by_proj.keys(), key=lambda pid: _go_live_key(proj_meta[pid][1]))
    best_version, best_go_live = proj_meta[best_proj_id]

    latest_rows = sorted(
        cases_by_proj[best_proj_id],
        key=lambda row: _updated_at_key(row[1]),
        reverse=True,
    )
    latest_cases = [row[0] for row in latest_rows]

    source_case_ids = [row[2] for row in latest_rows]

    return {
        "cases": latest_cases,
        "latest_go_live_date": best_go_live.isoformat() if best_go_live else None,
        "project_version": best_version,
        "source_case_ids": source_case_ids,
        "case_count": len(latest_cases),
        "version_refs": version_refs,
    }


async def consolidate_fp_cases(
    session,
    product_id: str,
    fp_name: str,
    version_id: str | None = None,
    fp_id: str | None = None,
) -> bool:
    """固化/更新某功能点的最新用例集记录（项目归档或纠正审核后调用）。

    Args:
        session: 数据库会话（调用方负责 commit）
        product_id: 产品 ID
        fp_name: 功能点名称（聚合键）
        version_id: 固化时的版本标识 "{project.version}-{project.name}"，
                  传入时追加到 version_refs 追溯链（同 version_id 幂等）。

    Returns:
        bool: 是否成功
    """
    try:
        live = await aggregate_latest_cases(session, product_id, fp_name, fp_id=fp_id)
        if live["case_count"] == 0:
            return True  # 无同名 FP/无 approved 用例，跳过（不报错）

        # 查询现有记录（按 product_id + fp_name 唯一约束）
        row = (
            await session.execute(
                select(FunctionalPointCases).where(
                    FunctionalPointCases.product_id == product_id,
                    FunctionalPointCases.fp_name == fp_name,
                )
            )
        ).scalar_one_or_none()

        refs: list[dict] = list(row.version_refs or []) if row else []
        if version_id:
            if not any(r.get("version_id") == version_id for r in refs):
                refs.append({
                    "version_id": version_id,
                    "snapshot_at": datetime.now(timezone.utc).isoformat(),
                    "source_case_ids": live["source_case_ids"],
                })

        if row:
            # 更新现有记录
            row.content = live["cases"]
            row.go_live_date = live["latest_go_live_date"]
            row.project_version = live["project_version"]
            row.source_case_ids = live["source_case_ids"]
            row.version_refs = refs
        else:
            # 新增记录
            session.add(FunctionalPointCases(
                product_id=product_id,
                fp_name=fp_name,
                content=live["cases"],
                go_live_date=live["latest_go_live_date"],
                project_version=live["project_version"],
                source_case_ids=live["source_case_ids"],
                version_refs=refs,
            ))

        logger.info(
            "[FPCases] 固化完成: product={}, fp={}, cases={}, version={}",
            product_id, fp_name, live["case_count"], version_id or "-",
        )
        return True
    except Exception as e:
        logger.error("[FPCases] 固化失败 product={} fp={}: {}", product_id, fp_name, str(e)[:200])
        return False


async def get_latest_cases(
    session,
    product_id: str,
    fp_name: str,
) -> list[dict] | None:
    """读取已固化的最新用例集（供 test_design 直接取用）。

    若不存在，返回 None，需现场 aggregate。
    """
    row = (
        await session.execute(
            select(FunctionalPointCases).where(
                FunctionalPointCases.product_id == product_id,
                FunctionalPointCases.fp_name == fp_name,
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    return row.content or []
