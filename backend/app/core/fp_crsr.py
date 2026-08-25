# -*- coding: utf-8 -*-
"""功能点权威完整版 SRR（CRSR）服务。

参考腾讯 Agent Memory L0-L3 分层思想，将"多项目版本分散完善"的同名功能点
聚合为一份最新最完整的整体 SRR：

  证据层（L0/L1）：各版本 functional_points.srr_json 原样保留，可回溯
  场景层（L2）  ：functional_point_crsr 表，项目归档时固化重建

聚合规则（用户确认）：
  - 标量字段（summary / suggested_module / fp_type / priority）→ 取最新非归档版本
  - 数组字段（business_rules / data_constraints / test_hints / flows.alternate_flows）
    → 内容签名去重并集（先最新，旧版本仅补充缺失项）
  - 同名同 ID 但内容冲突（如 business_rules 同 id 不同 rule）→ 以最新版本为准
  - non_functional dict → 最新版本优先，缺失键由旧版本补齐

消费方：
  - 依赖图谱节点点击（GET /products/fp-crsr/{fp_id}）
  - 需求影响分析受影响 FP 展开行
  - （后续）知识库问答向量条目 doc_type=fp_crsr
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import FunctionalPoint, FunctionalPointCRSR, Requirement
from app.utils.logger import logger

# 参与去重并集的数组字段
_LIST_KEYS = ("business_rules", "data_constraints", "test_hints")
# 标量字段（取最新）
_SCALAR_KEYS = ("summary", "suggested_module")


def _sig(item) -> str:
    """列表项内容签名（规范化 JSON 字符串），用于跨版本去重。"""
    try:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(item)


def _norm_text(s) -> str:
    """业务规则文本规范化（去除空白与中英文标点），用于跨版本同义规则合并。"""
    import re
    if not isinstance(s, str):
        return ""
    return re.sub(r"[\s\u3000「」【】()（）:：,，.。;；、\-—~～]", "", s)


def _item_markers(key: str, item) -> list[str]:
    """返回列表项用于跨版本去重的标记集合。

    - business_rules：取 [规范化 rule 文本, id]（任一命中即视为同一条规则），同 id 修订冲突以最新为准
    - data_constraints：取 (field, type, required, constraints) 语义键，**忽略 example**——
      避免同一字段约束因示例措辞不同而堆积成"一堆关闭原因"
    - 其余（test_hints）：按完整 JSON 签名
    """
    if key == "business_rules" and isinstance(item, dict):
        markers = []
        rule = item.get("rule")
        if isinstance(rule, str) and rule.strip():
            markers.append("R:" + _norm_text(rule))
        idx = item.get("id")
        if idx:
            markers.append("ID:" + str(idx))
        if markers:
            return markers
    if key == "data_constraints" and isinstance(item, dict):
        cons = item.get("constraints")
        sem = {
            "field": item.get("field"),
            "type": item.get("type"),
            "required": item.get("required"),
            # 约束文本规范化（去空白与标点，如"1-200"/"1~200"/"1 200"视为一致），避免措辞变体堆积
            "constraints": _norm_text(cons) if isinstance(cons, str) else cons,
        }
        sem = {k: v for k, v in sem.items() if v}
        return ["DC:" + json.dumps(sem, ensure_ascii=False, sort_keys=True)]
    return [_sig(item)]


def _merge_srr(srrs_latest_first: list[dict]) -> dict:
    """把多个版本的功能点 SRR 字段级合并为一个整体 CRSR。

    Args:
        srrs_latest_first: 按最新在前排序的各版本 srr_json 列表（可为空）

    Returns:
        dict: 聚合后的整体 SRR（深拷贝，不污染入参）
    """
    import copy

    if not srrs_latest_first:
        return {}

    merged = copy.deepcopy(srrs_latest_first[0] or {})

    for older in srrs_latest_first[1:]:
        older = older or {}
        # 1) 数组字段：语义去重并集（最新在前，旧版本只补缺）
        for key in _LIST_KEYS:
            newer_items = merged.get(key)
            older_items = older.get(key)
            if not isinstance(older_items, list) or not older_items:
                continue
            if not isinstance(newer_items, list):
                merged[key] = list(older_items)
                continue
            seen = set()
            for x in newer_items:
                seen.update(_item_markers(key, x))
            for item in older_items:
                marks = _item_markers(key, item)
                if any(m in seen for m in marks):
                    continue
                merged[key].append(item)
                seen.update(marks)

        # 2) flows：happy_path 取最新不动；alternate_flows 按 name 去重并集
        older_flows = older.get("flows")
        if isinstance(older_flows, dict):
            merged_flows = merged.get("flows")
            if not isinstance(merged_flows, dict):
                merged_flows = {}
            older_alt = older_flows.get("alternate_flows")
            if isinstance(older_alt, list) and older_alt:
                cur_alt = merged_flows.get("alternate_flows")
                if not isinstance(cur_alt, list):
                    cur_alt = []
                seen_names = {
                    str(a.get("name")) for a in cur_alt if isinstance(a, dict) and a.get("name")
                } | {_sig(a) for a in cur_alt}
                for a in older_alt:
                    name = str(a.get("name")) if isinstance(a, dict) and a.get("name") else None
                    if (name and name in seen_names) or _sig(a) in seen_names:
                        continue
                    cur_alt.append(a)
                    if name:
                        seen_names.add(name)
                merged_flows["alternate_flows"] = cur_alt
            merged["flows"] = merged_flows

        # 3) non_functional dict：缺失键由旧版本补齐（冲突以最新为主）
        older_nf = older.get("non_functional")
        if isinstance(older_nf, dict) and older_nf:
            cur_nf = merged.get("non_functional")
            if not isinstance(cur_nf, dict):
                cur_nf = {}
            for k, v in older_nf.items():
                if k not in cur_nf:
                    cur_nf[k] = v
            merged["non_functional"] = cur_nf

    return merged


def _sort_key(fp_row, req_version: int, is_archived: bool):
    """版本排序键：非归档在前；需求版本号降序；创建时间降序。"""
    return (
        1 if is_archived else 0,
        -req_version,
        -(fp_row.created_at.timestamp() if fp_row.created_at else 0),
    )


async def aggregate_crsr(
    session, product_id: str, fp_name: str
) -> dict:
    """实时聚合产品内同名功能点的整体 CRSR（不落库）。

    Returns:
        dict: {
            crsr: 聚合后整体 SRR,
            versions: 各版本归属列表（最新在前）,
            source_fp_ids: [fp_id...],
            version_count: int,
        }
    """
    rows = (
        await session.execute(
            select(
                FunctionalPoint.id,
                FunctionalPoint.name,
                FunctionalPoint.srr_json,
                FunctionalPoint.status,
                FunctionalPoint.created_at,
                Requirement.id,
                Requirement.title,
                Requirement.status,
                Requirement.version,
            )
            .join(Requirement, Requirement.id == FunctionalPoint.requirement_id)
            .where(
                Requirement.product_id == product_id,
                FunctionalPoint.name == fp_name,
            )
        )
    ).all()

    versions_meta = []
    srrs_latest_first = []
    source_fp_ids: list[str] = []

    decorated = []
    for (fid, fname, srr, fp_status, created_at, rid, rtitle, rstatus, rver) in rows:
        is_archived = fp_status == "archived" or rstatus == "archived"
        decorated.append((
            _sort_key(
                type("_R", (), {"created_at": created_at})(),
                int(rver or 1),
                is_archived,
            ),
            fid, fname, srr or {}, fp_status or "", created_at,
            rid, rtitle or "", rstatus or "", int(rver or 1), is_archived,
        ))
    decorated.sort(key=lambda x: x[0])

    for i, (_, fid, fname, srr, fp_status, created_at, rid, rtitle, rstatus, rver, is_arch) in enumerate(decorated):
        srrs_latest_first.append(srr)
        source_fp_ids.append(fid)
        versions_meta.append({
            "fp_id": fid,
            "fp_name": fname,
            "fp_status": fp_status,
            "req_id": rid,
            "req_title": rtitle,
            "req_status": rstatus,
            "req_version": rver,
            "created_at": created_at.isoformat() if created_at else "",
            "is_latest": i == 0,
            "is_archived": is_arch,
        })

    return {
        "crsr": _merge_srr(srrs_latest_first),
        "versions": versions_meta,
        "source_fp_ids": source_fp_ids,
        "version_count": len(versions_meta),
    }


async def get_crsr_view(session, fp_id: str) -> dict:
    """按 FP ID 获取 CRSR 视图（依赖图谱点击 / 影响分析展开共用）。

    优先实时聚合保证内容最新；同时返回已固化的 version_refs 供版本追溯。
    v3.3 P0-2: 追加 backlinks 反链数据（Obsidian 借鉴）——
    谁依赖此功能 / 此功能依赖谁 / 哪些用例引用了它，均带 product_id 隔离。
    """
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp:
        raise ValueError(f"功能点 {fp_id} 不存在")

    req = await session.get(Requirement, fp.requirement_id)
    if not req:
        raise ValueError(f"功能点 {fp_id} 所属需求不存在")
    product_id = req.product_id or ""

    live = await aggregate_crsr(session, product_id, fp.name)

    stored_refs: list[dict] = []
    stored_row = (
        await session.execute(
            select(FunctionalPointCRSR).where(
                FunctionalPointCRSR.product_id == product_id,
                FunctionalPointCRSR.fp_name == fp.name,
            )
        )
    ).scalar_one_or_none()
    if stored_row:
        stored_refs = list(stored_row.version_refs or [])

    # v3.3 P0-2: 反链计算——针对该功能点的所有版本 fp_id 查询依赖边
    backlinks = await _build_backlinks(session, product_id, fp.name, live["source_fp_ids"])

    return {
        "fp_id": fp_id,
        "fp_name": fp.name,
        "product_id": product_id,
        "crsr": live["crsr"],
        "versions": live["versions"],
        "version_count": live["version_count"],
        "source_fp_ids": live["source_fp_ids"],
        "stored_version_refs": stored_refs,
        "backlinks": backlinks,
    }


async def _build_backlinks(session, product_id: str, fp_name: str, source_fp_ids: list[str]) -> dict:
    """构建功能点反链视图（Obsidian Backlinks 借鉴）。

    - depended_by:        谁依赖此功能（反向边，本功能点任一版本 fp_id 作为 target）
    - references_fp:      此功能依赖谁（正向边，本功能点任一版本 fp_id 作为 source）
    - referenced_by_cases: 哪些用例引用了此功能（case_json.related_fp 按名称匹配，现有口径）
    - fan_in/fan_out:     approved+active 边计数（与影响分析口径一致）

    全部查询按 product_id 过滤（join requirements），防止跨产品数据泄漏；
    边按 FP 名称去重（同名跨版本视为同一功能）；非 approved 边也返回但标注中文状态。
    """
    from app.db.models import FPDependency, TestCase as TestCaseModel

    empty = {
        "depended_by": [], "references_fp": [], "referenced_by_cases": [],
        "fan_in": 0, "fan_out": 0,
    }
    if not source_fp_ids:
        return empty

    relation_cn = {
        "depends_on": "依赖",
        "data_flows": "数据流向",
        "invariants": "共享约束",
        "lifecycle_scenarios": "生命周期",
        "conflicts_with": "冲突",
    }
    review_cn = {"pending": "待审核", "approved": "已审核", "rejected": "已驳回"}

    depended_by: list[dict] = []
    references_fp: list[dict] = []
    fan_in = fan_out = 0
    seen_depended: set[str] = set()
    seen_refs: set[str] = set()

    try:
        # 反向边：谁依赖此功能（target 是本功能点任一版本）
        rev_result = await session.execute(
            select(FPDependency)
            .where(FPDependency.target_fp_id.in_(source_fp_ids))
        )
        rev_edges = rev_result.scalars().all()
        if rev_edges:
            src_ids = {e.source_fp_id for e in rev_edges}
            src_rows = await session.execute(
                select(
                    FunctionalPoint.id, FunctionalPoint.name,
                    FunctionalPoint.requirement_id,
                )
                .where(FunctionalPoint.id.in_(src_ids))
            )
            src_map = {r[0]: {"name": r[1], "req_id": r[2]} for r in src_rows.all()}
            # product_id 隔离：过滤掉跨产品的依赖者
            src_req_ids = {v["req_id"] for v in src_map.values() if v["req_id"]}
            req_rows = await session.execute(
                select(Requirement.id, Requirement.title, Requirement.product_id, Requirement.status)
                .where(Requirement.id.in_(src_req_ids))
            )
            src_req_map = {
                r[0]: {"title": r[1], "product_id": r[2], "status": r[3]}
                for r in req_rows.all()
            }
            for e in rev_edges:
                src = src_map.get(e.source_fp_id)
                if not src or not src.get("req_id"):
                    continue
                src_req = src_req_map.get(src["req_id"])
                if not src_req or src_req.get("product_id") != product_id:
                    continue  # 跨产品数据不泄漏
                if e.review_status == "approved" and e.is_active:
                    fan_in += 1
                # 按依赖者功能点名称去重（同名跨版本 = 同一功能）
                if src["name"] in seen_depended:
                    continue
                seen_depended.add(src["name"])
                depended_by.append({
                    "fp_id": e.source_fp_id,
                    "fp_name": src["name"],
                    "relation_cn": relation_cn.get(e.relation_type, e.relation_type),
                    "req_id": src["req_id"],
                    "req_title": src_req.get("title", ""),
                    "req_archived": src_req.get("status") == "archived",
                    "review_label": review_cn.get(e.review_status, e.review_status),
                    "is_active": bool(e.is_active),
                })

        # 正向边：此功能依赖谁（source 是本功能点任一版本）
        fwd_result = await session.execute(
            select(FPDependency)
            .where(FPDependency.source_fp_id.in_(source_fp_ids))
        )
        fwd_edges = fwd_result.scalars().all()
        if fwd_edges:
            tgt_ids = {e.target_fp_id for e in fwd_edges}
            tgt_rows = await session.execute(
                select(
                    FunctionalPoint.id, FunctionalPoint.name,
                    FunctionalPoint.requirement_id,
                )
                .where(FunctionalPoint.id.in_(tgt_ids))
            )
            tgt_map = {r[0]: {"name": r[1], "req_id": r[2]} for r in tgt_rows.all()}
            tgt_req_ids = {v["req_id"] for v in tgt_map.values() if v["req_id"]}
            tgt_req_rows = await session.execute(
                select(Requirement.id, Requirement.title, Requirement.product_id, Requirement.status)
                .where(Requirement.id.in_(tgt_req_ids))
            )
            tgt_req_map = {
                r[0]: {"title": r[1], "product_id": r[2], "status": r[3]}
                for r in tgt_req_rows.all()
            }
            for e in fwd_edges:
                tgt = tgt_map.get(e.target_fp_id)
                if not tgt or not tgt.get("req_id"):
                    continue
                tgt_req = tgt_req_map.get(tgt["req_id"])
                if not tgt_req or tgt_req.get("product_id") != product_id:
                    continue
                if e.review_status == "approved" and e.is_active:
                    fan_out += 1
                if tgt["name"] in seen_refs:
                    continue
                seen_refs.add(tgt["name"])
                references_fp.append({
                    "fp_id": e.target_fp_id,
                    "fp_name": tgt["name"],
                    "relation_cn": relation_cn.get(e.relation_type, e.relation_type),
                    "req_id": tgt["req_id"],
                    "req_title": tgt_req.get("title", ""),
                    "req_archived": tgt_req.get("status") == "archived",
                    "review_label": review_cn.get(e.review_status, e.review_status),
                    "is_active": bool(e.is_active),
                })
    except Exception as e:
        logger.warning("[CRSR] 反链计算失败（降级为空）: fp={}, {}", fp_name, str(e)[:150])

    # 用例引用：case_json.related_fp 按名称匹配（与影响分析 existing_cases_count 同口径）
    referenced_by_cases: list[dict] = []
    try:
        case_result = await session.execute(
            select(TestCaseModel.id, TestCaseModel.req_id, TestCaseModel.case_json)
            .where(TestCaseModel.product_id == product_id)
        )
        for row in case_result.all():
            case_json = row[2] or {}
            related_fp = case_json.get("related_fp", "") or case_json.get("functional_point", "") or ""
            if related_fp == fp_name:
                referenced_by_cases.append({
                    "case_id": row[0],
                    "title": case_json.get("title", ""),
                    "req_id": row[1] or "",
                    "case_type": case_json.get("type", ""),
                })
    except Exception as e:
        logger.warning("[CRSR] 用例引用查询失败（降级为空）: fp={}, {}", fp_name, str(e)[:150])

    return {
        "depended_by": depended_by,
        "references_fp": references_fp,
        "referenced_by_cases": referenced_by_cases[:20],  # 上限 20 条防撑爆面板
        "fan_in": fan_in,
        "fan_out": fan_out,
    }


async def consolidate_crsr(
    session,
    product_id: str,
    fp_name: str,
    version_id: str | None = None,
) -> bool:
    """固化/更新某功能点的 CRSR 记录（项目归档或纠正审核后调用）。

    Args:
        session: 数据库会话（调用方负责 commit）
        product_id: 产品 ID
        fp_name: 功能点名称（聚合键）
        version_id: 固化时的版本标识（{project.version}-{project.name}），
                    传入时追加到 version_refs 追溯链（同 version_id 幂等）

    Returns:
        bool: 是否成功
    """
    try:
        live = await aggregate_crsr(session, product_id, fp_name)
        if live["version_count"] == 0:
            return True  # 无同名 FP，跳过（可能已被删除）

        row = (
            await session.execute(
                select(FunctionalPointCRSR).where(
                    FunctionalPointCRSR.product_id == product_id,
                    FunctionalPointCRSR.fp_name == fp_name,
                )
            )
        ).scalar_one_or_none()

        refs: list[dict] = list(row.version_refs or []) if row else []
        if version_id:
            if not any(r.get("version_id") == version_id for r in refs):
                refs.append({
                    "version_id": version_id,
                    "snapshot_at": datetime.now(timezone.utc).isoformat(),
                    "source_fp_ids": live["source_fp_ids"],
                })

        if row:
            row.content = live["crsr"]
            row.source_fp_ids = live["source_fp_ids"]
            row.version_refs = refs
        else:
            session.add(FunctionalPointCRSR(
                product_id=product_id,
                fp_name=fp_name,
                content=live["crsr"],
                source_fp_ids=live["source_fp_ids"],
                version_refs=refs,
            ))
        logger.info(
            "[CRSR] 固化完成: product={}, fp={}, versions={}, ref={}",
            product_id, fp_name, live["version_count"], version_id or "-",
        )
        return True
    except Exception as e:
        logger.error("[CRSR] 固化失败 product={} fp={}: {}", product_id, fp_name, str(e)[:200])
        return False
