"""需求管理路由。

提供需求的上传解析、列表查询、详情查看、澄清问题获取与答案提交。

端点清单（实现计划 T5.2）：
    POST   /api/requirements/upload              上传需求，返回 req_id + 初始 SRR
    GET    /api/requirements                      需求列表（分页）
    GET    /api/requirements/{id}                 需求详情
    GET    /api/requirements/{id}/clarify/questions  获取待回答的澄清问题
    POST   /api/requirements/{id}/clarify         提交澄清答案

设计依据：
- docs/00-architecture-overview.md §2 访问层
- docs/01-requirement-parsing.md §3（ReqAgent 解析）+ §4（ClarifyAgent 澄清）
- D2 决策：Clarify 流程 API 驱动（不使用 LangGraph interrupt）

流程说明：
    1. 上传需求 → 调用 req_agent.parse 解析为 SRR → KB 检索历史需求 →
       ClarifyAgent 检测生成问题 → 保存为 draft 状态 → 返回问题列表
    2. （前端展示问题给用户选择/回答）
    3. 提交澄清答案 → 应用答案 → 重新调用 LLM 生成最终 SRR →
       状态更新为 active → 返回最终 SRR
    4. 后续调用 /api/generate 时，使用 active 状态的已完成 SRR
"""

from __future__ import annotations

from datetime import datetime, timezone, date
import asyncio
from typing import Any
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import Response
from sqlalchemy import func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin, require_write, get_current_user, CurrentUser
from app.core.operation_logger import log_operation
from app.agents.clarify_agent import clarify_agent
from app.agents.impact_agent import ImpactAnalysisAgent
from app.utils.id_gen import gen_requirement_id, gen_task_id
from app.agents.req_agent import req_agent
from app.db.database import get_session, async_session
from app.db.models import Requirement, Project, AnalysisTask, FunctionalPoint, FPDependency
from app.db.models import TestCase, GenerationBatch, ReqDependency, Traceability
from app.db.models import FPDependencyHistory  # v2.3: 依赖审核历史
from app.models.api_schemas import (
    ApiResponse,
    ClarifyAnswerRequest,
    RequirementSummary,
    RequirementUpdateRequest,
    RequirementUploadRequest,
)
from app.utils.logger import logger


# ============================================================================
# v1.5.1: FP 自动入库辅助函数
# ============================================================================

# v2.32: interaction_srr 独有的跨 FP 子字段（合并到 cross_fp_interactions 嵌套结构）
# 符合 SSOT 原则：总 SRR 的 requirement/flows 等字段不被 interaction_srr 覆盖
_INTERACTION_SUBKEYS: tuple[str, ...] = (
    "cross_fp_flows",
    "shared_business_rules",
    "fp_contracts",
    "global_constraints",
)


def _normalize_cross_fp_flow(item: dict) -> dict:
    """归一化 cross_fp_flows 条目，兼容 LLM 输出字段名差异。

    LLM 可能输出 from/to/contract 或 source_fp/target_fp/description，统一为后者。
    """
    if not isinstance(item, dict):
        return {}
    return {
        "name": item.get("name", "") or "",
        "source_fp": item.get("source_fp") or item.get("from", "") or "",
        "target_fp": item.get("target_fp") or item.get("to", "") or "",
        "interaction_type": item.get("interaction_type", "sequence") or "sequence",
        "description": item.get("description") or item.get("contract", "") or "",
    }


def _normalize_shared_business_rule(item: dict) -> dict:
    """归一化 shared_business_rules 条目，兼容 applies_to / applied_fps。"""
    if not isinstance(item, dict):
        return {}
    return {
        "id": item.get("id", "") or "",
        "rule": item.get("rule", "") or "",
        "applied_fps": item.get("applied_fps") or item.get("applies_to", []) or [],
    }


def _normalize_fp_contract(item: dict) -> dict:
    """归一化 fp_contracts 条目，兼容 fp_a/fp_b/contract 与 provider_fp/consumer_fp/contract_desc。"""
    if not isinstance(item, dict):
        return {}
    return {
        "provider_fp": item.get("provider_fp") or item.get("fp_a", "") or "",
        "consumer_fp": item.get("consumer_fp") or item.get("fp_b", "") or "",
        "data_fields": item.get("data_fields", []) or [],
        "contract_desc": item.get("contract_desc") or item.get("contract", "") or "",
    }


def _normalize_global_constraint(item: Any) -> str:
    """归一化 global_constraints 条目为字符串。

    LLM 可能输出字符串或 dict（如 {id, field, constraints}）。
    """
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        # 拼接 field + constraints，避免信息丢失
        field = item.get("field", "") or ""
        constraints = item.get("constraints", "") or item.get("rule", "") or ""
        desc = item.get("description", "") or ""
        parts = [p for p in [field, constraints, desc] if p]
        return "：".join(parts) if parts else ""
    return str(item).strip()


def _merge_interaction_srr(base_srr: dict, interaction_srr: dict) -> dict:
    """将 interaction_srr 选择性合并到 base_srr 的 cross_fp_interactions 嵌套结构（符合 SSOT 原则）。

    v2.32 重构：interaction_srr 的跨 FP 子字段不再写入 base_srr 顶层，
    而是合并到 base_srr["cross_fp_interactions"][子字段] 中，并做去重。
    总 SRR 已移除 acceptance_criteria 顶层字段，跨 FP AC 转为 global_constraints 字符串。

    合并规则：
        1. cross_fp_flows / shared_business_rules / fp_contracts → 追加到
           cross_fp_interactions 对应子字段（按归一化后的 JSON 字符串去重）
        2. global_constraints → 追加为字符串列表（按文本去重）
        3. acceptance_criteria → 转换为字符串后追加到 global_constraints（跨 FP AC 视为全局约束）
        4. requirement / flows / business_rules 等 → 不覆盖（SSOT 原则）

    Args:
        base_srr: 总 SRR（含 requirement/flows/business_rules 等完整字段）
        interaction_srr: 交互层 SRR（含 cross_fp_flows 等跨 FP 内容）

    Returns:
        dict: 合并后的 SRR（base_srr 的副本 + interaction_srr 的跨 FP 内容合并到 cross_fp_interactions）
    """
    import json as _json

    merged = dict(base_srr)

    # 确保 cross_fp_interactions 存在且为 dict
    cfi = merged.get("cross_fp_interactions")
    if not isinstance(cfi, dict):
        cfi = {}
    cfi.setdefault("cross_fp_flows", [])
    cfi.setdefault("shared_business_rules", [])
    cfi.setdefault("fp_contracts", [])
    cfi.setdefault("global_constraints", [])
    # 防御：确保子字段是 list
    for sub in _INTERACTION_SUBKEYS:
        if not isinstance(cfi.get(sub), list):
            cfi[sub] = []

    for key, value in interaction_srr.items():
        if not isinstance(value, list):
            continue

        if key == "cross_fp_flows":
            existing = cfi["cross_fp_flows"]
            seen = {_json.dumps(_normalize_cross_fp_flow(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_cross_fp_flow(item)
                if not norm.get("source_fp") and not norm.get("target_fp"):
                    continue
                sig = _json.dumps(norm, ensure_ascii=False, sort_keys=True)
                if sig not in seen:
                    existing.append(norm)
                    seen.add(sig)

        elif key == "shared_business_rules":
            existing = cfi["shared_business_rules"]
            seen = {_json.dumps(_normalize_shared_business_rule(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_shared_business_rule(item)
                if not norm.get("rule"):
                    continue
                sig = _json.dumps(norm, ensure_ascii=False, sort_keys=True)
                if sig not in seen:
                    existing.append(norm)
                    seen.add(sig)

        elif key == "fp_contracts":
            existing = cfi["fp_contracts"]
            seen = {_json.dumps(_normalize_fp_contract(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_fp_contract(item)
                if not norm.get("provider_fp") and not norm.get("consumer_fp"):
                    continue
                sig = _json.dumps(norm, ensure_ascii=False, sort_keys=True)
                if sig not in seen:
                    existing.append(norm)
                    seen.add(sig)

        elif key == "global_constraints":
            existing = cfi["global_constraints"]
            seen = {str(x).strip() for x in existing}
            for item in value:
                norm = _normalize_global_constraint(item)
                if norm and norm not in seen:
                    existing.append(norm)
                    seen.add(norm)

        elif key == "acceptance_criteria":
            # v2.32: 总 SRR 已移除 acceptance_criteria 顶层字段
            # 跨 FP AC 转换为字符串后追加到 global_constraints（视为全局约束）
            existing = cfi["global_constraints"]
            seen = {str(x).strip() for x in existing}
            for ac in value:
                if isinstance(ac, dict):
                    desc = ac.get("description", "") or ac.get("rule", "") or ""
                else:
                    desc = str(ac)
                desc = desc.strip()
                if desc and desc not in seen:
                    existing.append(f"[AC] {desc}")
                    seen.add(f"[AC] {desc}")
        # 其他键（requirement/flows/business_rules/data_constraints 等）：不覆盖

    merged["cross_fp_interactions"] = cfi
    return merged


async def _save_fps_to_db(
    session: AsyncSession, req_id: str, fps: list[dict], priority: str = "P2",
) -> int:
    """将 ReqAgent 提取的功能点列表存入 functional_points 表。

    每个 FP 的 sub_srr → srr_json，suggested_module 暂存不解析（用户手动分配模块）。
    返回实际入库的 FP 数量。
    """
    if not fps:
        return 0

    saved = 0
    for fp in fps:
        fp_id = gen_requirement_id()
        sub_srr = fp.get("sub_srr", {}) if isinstance(fp, dict) else {}

        record = FunctionalPoint(
            id=fp_id,
            requirement_id=req_id,
            name=((fp.get("name") or "未命名功能点") if isinstance(fp, dict) else "未命名功能点")[:255],
            fp_type=(
                fp.get("fp_type", "workflow") if isinstance(fp, dict) else "workflow"
            ),
            priority=priority,
            srr_json={
                **sub_srr,
                "summary": fp.get("summary", "") if isinstance(fp, dict) else "",
                "suggested_module": fp.get("suggested_module", "") if isinstance(fp, dict) else "",
            },
            atomicity_check=(
                fp.get("atomicity_check", {}) if isinstance(fp, dict) else {}
            ),
            test_hints=(
                fp.get("test_hints", []) if isinstance(fp, dict) else []
            ),
            status="pending_review",
        )
        session.add(record)
        saved += 1

    await session.flush()  # P0-1: 确保 FP 写入 DB，_save_fp_dependencies 可查询到
    logger.info("[FP-AutoSave] 需求 {} 自动存入 {} 个功能点", req_id, saved)
    return saved


async def _bump_req_revision(session: AsyncSession, req_id: str) -> None:
    """v2.33: 自增需求的 revision 字段，用于多人协作冲突检测。

    在所有修改 srr_json 的地方调用此函数，让前端轮询发现 revision 变化时提示刷新。

    Args:
        session: 数据库会话
        req_id: 需求 ID
    """
    from sqlalchemy import update as sa_update
    await session.execute(
        sa_update(Requirement).where(Requirement.id == req_id).values(
            revision=Requirement.revision + 1,
        )
    )


async def _update_cross_fp_interactions_on_merge(
    session: AsyncSession, req_id: str, old_names: set[str], new_name: str,
) -> None:
    """v2.33: 功能点合并后增量更新 cross_fp_interactions 中的旧 FP 名称引用。

    将 cross_fp_interactions 的四个子字段中引用的旧 FP 名称替换为合并后的新名称：
    - cross_fp_flows: source_fp / target_fp
    - shared_business_rules: applied_fps[]
    - fp_contracts: provider_fp / consumer_fp
    - global_constraints: 不涉及（是文本约束，不引用 FP 名称）

    Args:
        session: 数据库会话
        req_id: 需求 ID
        old_names: 被合并的旧 FP 名称集合
        new_name: 合并后的新 FP 名称
    """
    if not old_names or not new_name:
        return

    from sqlalchemy import select as sa_select, update as sa_update

    result = await session.execute(
        sa_select(Requirement.srr_json).where(Requirement.id == req_id)
    )
    req_row = result.first()
    if not req_row or not req_row[0]:
        return

    srr_json = dict(req_row[0]) if isinstance(req_row[0], dict) else {}
    cfi = srr_json.get("cross_fp_interactions")
    if not isinstance(cfi, dict):
        return

    changed = False

    # 1. cross_fp_flows: 替换 source_fp / target_fp
    flows = cfi.get("cross_fp_flows")
    if isinstance(flows, list):
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            for key in ("source_fp", "target_fp"):
                val = flow.get(key, "")
                if val in old_names:
                    flow[key] = new_name
                    changed = True

    # 2. shared_business_rules: 替换 applied_fps[]
    shared_rules = cfi.get("shared_business_rules")
    if isinstance(shared_rules, list):
        for rule in shared_rules:
            if not isinstance(rule, dict):
                continue
            applied = rule.get("applied_fps")
            if isinstance(applied, list):
                new_applied = []
                for fp_name in applied:
                    if fp_name in old_names:
                        new_applied.append(new_name)
                        changed = True
                    else:
                        new_applied.append(fp_name)
                # 去重（合并后可能产生重复）
                seen = set()
                deduped = []
                for name in new_applied:
                    if name not in seen:
                        seen.add(name)
                        deduped.append(name)
                if len(deduped) != len(new_applied):
                    new_applied = deduped
                rule["applied_fps"] = new_applied

    # 3. fp_contracts: 替换 provider_fp / consumer_fp
    contracts = cfi.get("fp_contracts")
    if isinstance(contracts, list):
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            for key in ("provider_fp", "consumer_fp"):
                val = contract.get(key, "")
                if val in old_names:
                    contract[key] = new_name
                    changed = True

    if changed:
        srr_json["cross_fp_interactions"] = cfi
        await session.execute(
            sa_update(Requirement).where(Requirement.id == req_id).values(srr_json=srr_json)
        )
        logger.info(
            "[FP-Merge] cross_fp_interactions 已增量更新: req_id={}, old_names={}, new_name={}",
            req_id, old_names, new_name,
        )


async def _update_fp_index_in_srr(session: AsyncSession, req_id: str) -> None:
    """更新总 SRR 中的 fp_index 和 fp_summary 字段（FP 引用列表 + FP 概要，符合 SSOT 追溯原则）。

    从 functional_points 表查询当前需求的所有 FP，构建 fp_index 和 fp_summary 写入 srr_json。
    - fp_index 只包含 FP 的引用信息（id/name/fp_type/status），不包含 FP 内部 SRR 内容。
    - fp_summary 包含 FP 的概要信息（name/fp_type/priority/summary/module），由后端基于实际 FP 生成。

    Args:
        session: 数据库会话
        req_id: 需求 ID
    """
    from sqlalchemy import select as sa_select, update as sa_update

    result = await session.execute(
        sa_select(
            FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
            FunctionalPoint.status, FunctionalPoint.priority,
            FunctionalPoint.srr_json, FunctionalPoint.module_id,
        )
        .where(FunctionalPoint.requirement_id == req_id)
        .order_by(FunctionalPoint.created_at)
    )
    fp_rows = result.all()

    fp_index = [
        {"fp_id": row[0], "name": row[1], "fp_type": row[2], "status": row[3]}
        for row in fp_rows
    ]

    # v2.22: 构建 fp_summary（基于实际入库的 FP，覆盖 LLM 首次输出的 fp_summary）
    fp_summary = []
    for row in fp_rows:
        srr_json = row[5] if isinstance(row[5], dict) else {}
        # summary 可能在 srr_json.summary 或 srr_json.requirement.summary
        req_obj = srr_json.get("requirement", {})
        summary_text = srr_json.get("summary", "") or (req_obj.get("summary", "") if isinstance(req_obj, dict) else "")
        fp_summary.append({
            "name": row[1] or "",
            "fp_type": row[2] or "workflow",
            "priority": row[4] or "P2",
            "summary": summary_text or "",
            "module": row[6] or "",
        })

    # 读取当前 srr_json，更新 fp_index 和 fp_summary 字段
    req_result = await session.execute(
        sa_select(Requirement.srr_json).where(Requirement.id == req_id)
    )
    req_row = req_result.first()
    if req_row and req_row[0]:
        srr_json = dict(req_row[0]) if isinstance(req_row[0], dict) else {}
        srr_json["fp_index"] = fp_index
        srr_json["fp_summary"] = fp_summary
        await session.execute(
            sa_update(Requirement).where(Requirement.id == req_id).values(srr_json=srr_json)
        )
        # v2.33: 自增 revision（FP 列表变化属于内容变更）
        await _bump_req_revision(session, req_id)
        logger.info("[FP-Index] 已更新需求 {} 的 fp_index + fp_summary: {} 个 FP", req_id, len(fp_index))


async def _save_fp_dependencies(
    session: AsyncSession, req_id: str, deps: list[dict],
) -> int:
    """将 LLM 提取的 FP 依赖关系存入 fp_dependencies 表。

    依赖关系中的 source_fp_name/target_fp_name 需先映射为实际的 FP ID。
    返回成功创建的依赖条数。
    """
    if not deps:
        return 0

    # 加载该需求下所有 FP 的 name→id 映射
    fp_result = await session.execute(
        select(FunctionalPoint.id, FunctionalPoint.name).where(
            FunctionalPoint.requirement_id == req_id
        )
    )
    name_to_id = {row[1]: row[0] for row in fp_result.fetchall()}

    saved = 0
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        src_name = dep.get("source_fp_name", "")
        tgt_name = dep.get("target_fp_name", "")
        src_id = name_to_id.get(src_name)
        tgt_id = name_to_id.get(tgt_name)
        if not src_id or not tgt_id or src_id == tgt_id:
            continue
        relation = dep.get("relation_type", "depends_on")
        if relation not in ("depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"):
            relation = "depends_on"
        # 查重
        dup = await session.execute(
            select(FPDependency).where(
                FPDependency.source_fp_id == src_id,
                FPDependency.target_fp_id == tgt_id,
                FPDependency.relation_type == relation,
            )
        )
        if dup.scalar_one_or_none():
            continue
        session.add(FPDependency(
            source_fp_id=src_id,
            target_fp_id=tgt_id,
            relation_type=relation,
            description=dep.get("description", ""),
        ))
        saved += 1

    if saved:
        logger.info("[FP-Dep] 需求 {} 自动创建 {} 条依赖关系", req_id, saved)
    return saved


# ============================================================================
# 文件校验辅助函数
# ============================================================================

_DOCX_MAGIC = b"PK\x03\x04"  # Office Open XML 文件头（.docx）
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _validate_file_magic(ext: str, raw_bytes: bytes) -> None:
    """通过魔数（magic bytes）校验文件类型，抵御篡改扩展名攻击。

    Args:
        ext: 扩展名（小写，不含点号）
        raw_bytes: 文件前 N 个字节

    Raises:
        HTTPException 400: 魔数校验不通过
    """
    if ext == "docx":
        if not raw_bytes.startswith(_DOCX_MAGIC):
            raise HTTPException(
                status_code=400,
                detail="文件格式不匹配：扩展名为 .docx 但文件头不是 Office Open XML 格式",
            )
    # .md / .markdown：魔数校验意义不大，靠 UTF-8/GBK 解码校验


def _table_to_markdown(table) -> str:
    """将 docx 表格转为 Markdown 表格语法（含表头分隔行）。

    Args:
        table: python-docx 的 Table 对象

    Returns:
        str: Markdown 表格文本（空表格返回空串）
    """
    rows_data: list[list[str]] = []
    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ").replace("|", "\\|") for cell in row.cells]
        rows_data.append(cells)
    if not rows_data:
        return ""
    # 去除完全空行
    rows_data = [r for r in rows_data if any(c.strip() for c in r)]
    if not rows_data:
        return ""
    col_count = max(len(r) for r in rows_data)
    # 补齐每行列数
    rows_data = [r + [""] * (col_count - len(r)) for r in rows_data]
    lines = []
    for i, row_cells in enumerate(rows_data):
        lines.append("| " + " | ".join(row_cells) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * col_count) + "|")
    return "\n".join(lines)


async def _extract_docx_text(
    raw_bytes: bytes, filename: str = "", session=None
) -> tuple[str, list[dict]]:
    """从 docx 提取文本，保持段落与表格的原始顺序，识别图片/流程图占位。

    v2.18: 修复 3 个 Bug + 新增多模态识别 + 原始图片保存：
    1. 表格在段落不为空时被丢弃 → 改为按文档顺序始终提取表格
    2. 多文件上传端点完全不读表格 → 统一调用本函数
    3. 图片/流程图完全不可见 → 检测图片并插入占位提示
    4. 多模态识别：若配置了视觉 LLM，图片自动交给多模态模型生成内容描述
    5. 原始图片保存：提取的图片 base64 数据同时返回，供前端展示对比

    Args:
        raw_bytes: docx 文件字节
        filename: 文件名（仅用于日志）
        session: PG 异步会话（用于读取视觉 LLM 供应商配置）。None 时跳过多模态识别

    Returns:
        tuple[str, list[dict]]: (提取的纯文本, 原始图片信息列表)
        图片信息: {"index": int, "data_url": str, "content_type": str}

    Raises:
        ImportError: python-docx 未安装
        Exception: 解析失败
    """
    from io import BytesIO
    import base64
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(BytesIO(raw_bytes))
    body = doc.element.body

    # 构建 XML 元素 → python-docx 对象映射（用于按文档顺序遍历时反查对象）
    para_map = {p._element: p for p in doc.paragraphs}
    table_map = {t._element: t for t in doc.tables}

    # 读取多模态 LLM 供应商配置（若 session 可用）
    vision_provider: str | None = None
    if session is not None:
        try:
            from app.api.knowledge import get_vision_llm_provider
            vision_provider = await get_vision_llm_provider(session)
        except Exception as e:
            logger.warning("[API/requirements] 读取视觉 LLM 供应商失败，图片将使用占位提示: {}", str(e)[:120])

    parts: list[str] = []
    raw_images: list[dict] = []
    table_count = 0
    image_count = 0
    vision_success = 0
    vision_fail = 0

    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            # 段落
            para = para_map.get(child)
            if para and para.text.strip():
                parts.append(para.text)
            # 检测段落中的图片/绘图（DrawingML 或 VML 格式）
            drawings = child.findall(".//" + qn("w:drawing"))
            picts = child.findall(".//" + qn("w:pict"))
            img_elements = drawings + picts
            if img_elements:
                for img_elem in img_elements:
                    image_count += 1
                    description, image_info = await _describe_docx_image(
                        doc, img_elem, image_count, vision_provider
                    )
                    if description.startswith("【图片"):
                        vision_fail += 1
                    else:
                        vision_success += 1
                    parts.append(description)
                    # 保存原始图片数据供前端展示
                    if image_info:
                        raw_images.append(image_info)
        elif tag == qn("w:tbl"):
            # 表格
            table = table_map.get(child)
            if table:
                table_count += 1
                md = _table_to_markdown(table)
                if md:
                    parts.append(md)

    text = "\n".join(parts)
    logger.info(
        "[API/requirements] docx 解析: filename={}, text_len={}, tables={}, images={}, "
        "vision_ok={}, vision_fail={}, raw_images_saved={}",
        filename, len(text), table_count, image_count, vision_success, vision_fail,
        len(raw_images),
    )
    return text, raw_images


async def _describe_docx_image(
    doc, img_elem, img_index: int, vision_provider: str | None
) -> tuple[str, dict | None]:
    """提取 docx 中图片字节并调用多模态 LLM 生成描述。

    Args:
        doc: python-docx Document 对象
        img_elem: 图片 XML 元素（w:drawing 或 w:pict）
        img_index: 图片序号（用于占位提示）
        vision_provider: 视觉 LLM 供应商名称。None 时直接返回占位提示

    Returns:
        tuple[str, dict | None]: (图片描述文本, 原始图片信息)
        原始图片信息: {"index": int, "data_url": str, "content_type": str} 或 None
    """
    # 从 XML 提取图片字节：查找 a:blip 的 r:embed 属性 → 通过关系 ID 获取图片 part
    try:
        from docx.oxml.ns import qn as _qn
        blips = img_elem.findall(".//" + _qn("a:blip"))
        if not blips:
            return f"【图片{img_index}：检测到图片但无法定位图片数据】", None

        descriptions: list[str] = []
        image_info: dict | None = None
        for blip in blips:
            embed_attr = blip.get(_qn("r:embed"))
            if not embed_attr:
                continue
            # 通过关系 ID 获取图片 part
            image_part = doc.part.related_parts.get(embed_attr)
            if not image_part or not hasattr(image_part, "blob"):
                continue
            image_bytes = image_part.blob
            if not image_bytes:
                continue

            # 转为 base64
            import base64
            img_b64 = base64.b64encode(image_bytes).decode("utf-8")

            # 推断 MIME 类型
            content_type = getattr(image_part, "content_type", "image/png")
            mime_type = content_type if content_type else "image/png"

            # v2.18: 保存原始图片数据（无论多模态是否配置，都保存原始图片供前端展示）
            image_info = {
                "index": img_index,
                "data_url": f"data:{mime_type};base64,{img_b64}",
                "content_type": mime_type,
            }

            if vision_provider is None:
                descriptions.append(
                    f"【图片{img_index}：此处包含图片/流程图/示意图，"
                    f"未配置多模态 LLM，请人工补充内容描述】"
                )
            else:
                # 调用多模态 LLM 识别图片内容
                # v2.18: 提示词从 prompt_manager 获取，支持 DB 覆盖和手动优化
                from app.core import prompt_manager
                vision_system_prompt = prompt_manager.get("vision", "SYSTEM_PROMPT")
                vision_user_prompt = prompt_manager.get("vision", "USER_PROMPT_TEMPLATE")
                from app.core.llm_client import llm_client
                description = await llm_client.call_vision(
                    image_base64=img_b64,
                    prompt=vision_user_prompt,
                    system_prompt=vision_system_prompt,
                    temperature=0.2,
                    provider=vision_provider,
                    mime_type=mime_type,
                )
                descriptions.append(f"【图片{img_index}内容描述（多模态识别）】\n{description}")

        if descriptions:
            return "\n".join(descriptions), image_info
        return f"【图片{img_index}：检测到图片但无法提取图片数据】", None
    except Exception as e:
        logger.warning(
            "[API/requirements] 多模态图片识别失败 [index={}]: {}",
            img_index, str(e)[:200],
        )
        return (
            f"【图片{img_index}：多模态识别失败（{str(e)[:80]}），"
            f"请人工补充该图片的内容描述】"
        ), None


async def _parse_uploaded_files(files: list[UploadFile], session=None) -> tuple[str, list[dict]]:
    """解析上传的需求文件列表，合并为纯文本。

    复用 upload_requirement_file 的解析逻辑（扩展名校验 / 魔数校验 / md/txt/docx 解析），
    供 regenerate 等需要文件上传的端点复用。

    Args:
        files: UploadFile 列表（支持 .md / .markdown / .txt / .docx）
        session: PG 异步会话（用于 docx 多模态图片识别）

    Returns:
        tuple[str, list[dict]]: (合并后的纯文本, 所有 docx 文件提取的原始图片列表)
        多文件以分隔线连接

    Raises:
        HTTPException 400: 文件格式不支持 / 内容为空 / 解析失败
    """
    MAX_FILE_SIZE = 10 * 1024 * 1024
    parts: list[str] = []
    all_images: list[dict] = []

    for file in files:
        filename = file.filename or ""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

        if ext not in ("md", "markdown", "docx", "txt"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: .{ext}，仅支持 .md / .docx / .txt",
            )

        raw_bytes = await file.read()
        if len(raw_bytes) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"文件 {filename} 大小超过 10MB 上限，请压缩后重试",
            )

        _validate_file_magic(ext, raw_bytes)

        file_text = ""
        if ext in ("md", "markdown", "txt"):
            try:
                file_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    file_text = raw_bytes.decode("gbk")
                except UnicodeDecodeError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件 {filename} 编码不支持，请使用 UTF-8 或 GBK 编码",
                    )
        elif ext == "docx":
            try:
                file_text, doc_images = await _extract_docx_text(raw_bytes, filename, session=session)
                all_images.extend(doc_images)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="python-docx 未安装，无法解析 .docx 文件",
                )
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"文件 {filename} 解析失败: {str(e)[:150]}",
                )

        if not file_text or not file_text.strip():
            raise HTTPException(status_code=400, detail=f"文件 {filename} 内容为空")

        logger.info("[API/requirements] 文件解析完成: filename={}, text_len={}", filename, len(file_text))
        parts.append(file_text)

    # 多文件以分隔线连接，保持各文件内容边界清晰
    return "\n\n---\n\n".join(parts), all_images


router = APIRouter()


# ============================================================================
# 1. 上传需求
# ============================================================================

@router.post("/upload", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement(
    req: RequirementUploadRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """上传需求文本（异步任务模式，与文件上传统一）。

    v2.15: 改为异步任务模式。立即创建 Requirement(analyzing) + AnalysisTask，
    后台协程执行 ReqAgent 解析 + ClarifyAgent 检测。立即返回 task_id + req_id，
    前端轮询任务进度（GET /requirements/tasks/{task_id}）。

    Args:
        req: 上传请求，含 raw_text / product_id / product_name / title / priority / modules
        session: 数据库会话（依赖注入）

    Returns:
        ApiResponse: data 含 task_id / req_id / status="analyzing"
    """
    if not req.raw_text or not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="需求文本不能为空")

    logger.info(
        "[API/requirements] 上传需求(raw_text 异步模式): text_len={}, product_id={}, title={}",
        len(req.raw_text),
        req.product_id,
        req.title or "(自动提取)",
    )

    # v2.15: 异步分析模式——立即创建 Requirement(analyzing) + AnalysisTask，后台协程执行解析
    req_id = gen_requirement_id()
    task_id = gen_task_id()

    # 立即创建 Requirement 记录（status=analyzing，分析完成后由后台协程更新）
    record = Requirement(
        id=req_id,
        product_id=req.product_id or "PROD-DEFAULT",
        project_id=req.project_id,
        title=req.title or "(分析中...)",
        priority=req.priority,
        module=req.modules[0] if req.modules else "",
        modules=req.modules,
        tags=[],
        version=1,
        status="analyzing",
        srr_json={},
        raw_text=req.raw_text,
        source_type="markdown",
        created_by="api",
    )
    session.add(record)
    await session.flush()  # 确保 Requirement 先落库，避免 AnalysisTask 外键约束 violation

    # 创建任务记录
    task = AnalysisTask(
        id=task_id,
        task_type="upload_requirement",
        req_id=req_id,
        product_id=req.product_id or "PROD-DEFAULT",
        status="pending",
        phase="parse",
        progress=0,
        message="已接收需求文本，准备开始分析...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）
    asyncio.create_task(_run_requirement_analysis(
        task_id=task_id,
        req_id=req_id,
        combined_raw=req.raw_text,
        product_id=req.product_id or "PROD-DEFAULT",
        product_name=req.product_name,
        title=req.title,
        priority=req.priority,
        project_id=req.project_id or "",
        module_list=req.modules,
        source_files=[],
    ))

    logger.info("[API] raw_text 上传已派发异步分析: task_id={}, req_id={}", task_id, req_id)

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="upload_requirement", target_type="requirement", target_id=req_id,
        detail={"title": req.title or "(自动提取)", "product_id": req.product_id or "PROD-DEFAULT"},
    )

    return ApiResponse(
        success=True,
        message="已接收需求文本，正在后台分析",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
        },
    )


# ============================================================================
# 1b. 文件上传需求（支持 .md / .docx）
# ============================================================================

@router.post("/upload/file", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement_file(
    file: UploadFile = File(..., description="需求文件（.md 或 .docx）"),
    product_id: str = Form(default="PROD-DEFAULT", description="产品 ID"),
    product_name: str = Form(default="", description="产品名称"),
    title: str = Form(default="", description="需求标题（可选，留空则从内容提取）"),
    priority: str = Form(default="P2", description="优先级 P0-P3"),
    project_id: str = Form(default="", description="项目/版本 ID（可选）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """上传需求文件（.md 或 .docx），自动解析文本后调用 ReqAgent 生成 SRR。

    支持的文件格式：
    - .md / .markdown：直接读取文本内容
    - .docx：使用 python-docx 解析为纯文本

    安全校验：
    - 文件扩展名白名单（仅 .md / .docx）
    - MIME 类型二次校验（字段检查 + 魔数检查）
    - 文件大小上限：10MB（避免内存溢出）

    设计依据：
    - docs/01-requirement-parsing.md §3（ReqAgent 解析流程）
    - docs/00 §2 访问层：文件上传是标准的接入方式
    """
    # 1. 校验文件扩展名
    filename = file.filename or ""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext not in ("md", "markdown", "docx", "txt"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: .{ext}，仅支持 .md / .docx / .txt",
        )

    # 1.5 文件大小检查（10MB 上限）
    MAX_FILE_SIZE = 10 * 1024 * 1024
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="文件大小超过 10MB 上限，请压缩后重试",
        )

    logger.info(
        "[API/requirements] 上传文件: filename={}, ext=.{}, size={}, product_id={}, title={}",
        filename,
        ext,
        len(raw_bytes),
        product_id,
        title or "(自动提取)",
    )

    # 2. MIME 类型魔数校验（抵御篡改扩展名的攻击）
    _validate_file_magic(ext, raw_bytes)

    # 3. 读取并解析文件内容
    raw_text = ""
    raw_images: list[dict] = []

    if ext in ("md", "markdown", "txt"):
        # 直接读取为 UTF-8 文本
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # 尝试 GBK 编码（Windows 常见）
            try:
                raw_text = raw_bytes.decode("gbk")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="文件编码不支持，请使用 UTF-8 或 GBK 编码的 .md 文件",
                )
    elif ext == "docx":
        # 使用 python-docx 解析（v2.18: 统一调用 _extract_docx_text，保留表格和图片占位）
        try:
            raw_text, raw_images = await _extract_docx_text(raw_bytes, filename=file.filename, session=session)
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="python-docx 未安装，无法解析 .docx 文件。请执行: pip install python-docx",
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f".docx 文件解析失败: {str(e)[:150]}",
            )

    if not raw_text or not raw_text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")

    logger.info("[API/requirements] 文件解析完成: raw_text_len={}", len(raw_text))

    # 3. 调用 ReqAgent 解析需求为 SRR
    try:
        srr = await req_agent.parse(
            raw_requirement=raw_text,
            product_id=product_id,
            product_name=product_name,
        )
    except Exception as e:
        logger.error("[API/requirements] 需求解析失败: {}", str(e)[:200])
        raise HTTPException(
            status_code=500,
            detail=f"需求解析失败: {str(e)[:200]}",
        )

    req_id = srr.get("requirement", {}).get("id", "")
    if not req_id:
        raise HTTPException(status_code=500, detail="解析失败：未生成需求 ID")

    # 确保 req_id 唯一（防止 LLM 生成重复 ID）
    existing = await session.get(Requirement, req_id)
    if existing:
        # 重新生成唯一 ID（线程安全计数器，避免并发冲突）
        req_id = gen_requirement_id()
        srr["requirement"]["id"] = req_id
        logger.warning("[API/requirements] req_id 冲突 (file)，重新生成: new_id={}", req_id)

    # v1.5.1: 自动保存 FP
    req_data = srr.setdefault("requirement", {})
    if title:
        req_data["title"] = title
    if priority:
        req_data["priority"] = priority

    # P0 修复：先创建并 flush Requirement 记录，避免 _save_fps_to_db 时外键违反
    # v2.18: 合并 interaction_srr 到总 SRR（统一所有流程的 interaction_srr 处理）
    final_srr = srr
    if req_agent.interaction_srr and isinstance(req_agent.interaction_srr, dict):
        final_srr = _merge_interaction_srr(srr, req_agent.interaction_srr)

    record = Requirement(
        id=req_id,
        product_id=req_data.get("product_id", product_id),
        project_id=project_id or None,
        title=req_data.get("title", ""),
        priority=req_data.get("priority", "P2"),
        module=req_data.get("modules", [""])[0] if req_data.get("modules") else "",
        modules=req_data.get("modules", []),
        tags=[],
        version=1,
        status="active",
        srr_json=final_srr,
        raw_text=raw_text,
        raw_images=raw_images,
        source_type=ext,
        created_by="api",
    )
    session.add(record)
    await session.flush()

    await _save_fps_to_db(session, req_id, req_agent.last_fps, priority)
    await _save_fp_dependencies(session, req_id, req_agent.last_fp_dependencies)
    await _update_fp_index_in_srr(session, req_id)  # v2.18: 更新总 SRR 的 fp_index
    await session.commit()

    logger.info(
        "[API/requirements] 文件上传需求已保存: req_id={}, title={}, source=file.{}",
        req_id,
        req_data.get("title", ""),
        ext,
    )

    return ApiResponse(
        success=True,
        message=f".{ext} 文件需求解析成功",
        data={
            "req_id": req_id,
            "srr": srr,
            "title": req_data.get("title", ""),
            "priority": req_data.get("priority", ""),
            "source_file": filename,
        },
    )


# ============================================================================
# 1c. 异步分析后台协程（v2.14 新增 - 需求解析/澄清的异步执行）
# ============================================================================

async def _update_task(task_id: str, **fields) -> None:
    """更新 AnalysisTask 记录（独立 session，供后台协程使用）。

    v2.23: 自动追踪 phase_timings — 当 phase 变化或任务完成/失败/中断时，
    自动记录阶段的开始/结束/耗时（ms），供前端展示每步操作用时。

    v2.23.1 修复：当同一次调用同时设置新 phase 和终止 status 时，
    需要关闭新开启的阶段（而非已关闭的旧阶段）。
    """
    try:
        async with async_session() as s:
            from sqlalchemy import update as sa_update

            # 读取当前任务，判断 phase 是否变化
            task = await s.get(AnalysisTask, task_id)
            if task is None:
                return

            timings = dict(task.phase_timings or {})
            new_phase = fields.get("phase")
            new_status = fields.get("status")
            now = datetime.now(timezone.utc)

            def _close_phase(phase_key: str) -> None:
                """关闭指定阶段（记录 end + duration_ms），若已关闭或无 start 则跳过。"""
                if not phase_key or phase_key not in timings:
                    return
                phase_data = dict(timings[phase_key])
                if "end" in phase_data or "start" not in phase_data:
                    return
                try:
                    start_dt = datetime.fromisoformat(phase_data["start"])
                    phase_data["end"] = now.isoformat()
                    phase_data["duration_ms"] = int((now - start_dt).total_seconds() * 1000)
                    timings[phase_key] = phase_data
                except (ValueError, TypeError):
                    pass

            # 1. phase 变化时：关闭前一阶段，开启新阶段
            phase_changed = bool(new_phase) and new_phase != task.phase
            if phase_changed:
                _close_phase(task.phase)
                timings[new_phase] = {"start": now.isoformat()}

            # 2. 任务完成/失败/中断时：关闭当前活跃阶段
            #    注意：若刚切换了 phase，应关闭新开启的阶段
            if new_status in ("completed", "failed", "interrupted"):
                active_phase = new_phase if phase_changed else task.phase
                _close_phase(active_phase)

            # 只在有 phase_timings 变更时写入字段
            if phase_changed or new_status in ("completed", "failed", "interrupted"):
                fields["phase_timings"] = timings

            stmt = sa_update(AnalysisTask).where(AnalysisTask.id == task_id).values(**fields)
            await s.execute(stmt)
            await s.commit()
    except Exception as e:
        logger.warning("[Task] 更新任务状态失败: task_id={}, error={}", task_id, str(e)[:150])


async def _run_requirement_analysis(
    task_id: str,
    req_id: str,
    combined_raw: str,
    product_id: str,
    product_name: str,
    title: str,
    priority: str,
    project_id: str,
    module_list: list[str],
    source_files: list[str],
    file_bytes_list: list[tuple[str, bytes]] | None = None,
) -> None:
    """后台执行需求解析 + 澄清（异步协程，使用独立 session）。

    流程：
    0. phase=upload: 文件解析（docx/md/txt → combined_raw）
    1. phase=parse: req_agent.parse() 解析需求
    2. phase=kb_search: clarify_agent.clarify() KB 检索 + 问题生成
    3. 若有问题: phase=clarify, status=completed, result_json 含 questions
       若无问题: phase=regenerate, 生成 final_srr, Requirement.status=active
    """
    from app.db.models import Requirement as ReqModel
    from sqlalchemy import update as sa_update

    async def _is_cancelled() -> bool:
        """检测任务是否被用户取消（status=interrupted）。"""
        try:
            async with async_session() as s:
                t = await s.get(AnalysisTask, task_id)
                return t is not None and t.status == "interrupted"
        except Exception:
            return False

    try:
        # ===== phase 0: upload（文件解析）=====
        # v2.24: 文件解析从 API 请求移到协程中，任务记录可提前创建
        if file_bytes_list:
            await _update_task(task_id, status="running", phase="upload", progress=3,
                               message=f"正在解析 {len(file_bytes_list)} 个文件...")
            all_raw_images: list[dict] = []
            merged_parts: list[str] = []
            for filename, raw_bytes in file_bytes_list:
                ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
                if ext in ("md", "markdown", "txt"):
                    text = raw_bytes.decode("utf-8", errors="replace")
                elif ext == "docx":
                    try:
                        text, doc_images = await _extract_docx_text(raw_bytes, filename=filename)
                        all_raw_images.extend(doc_images)
                    except Exception as e:
                        raise RuntimeError(f"解析 {filename} 失败: {str(e)[:200]}")
                else:
                    text = raw_bytes.decode("utf-8", errors="replace")
                merged_parts.append(f"【文档：{filename}】\n{text}")
            combined_raw = "\n\n---\n\n".join(merged_parts)

            # 保存 raw_text 和 raw_images 到 Requirement
            async with async_session() as s:
                await s.execute(sa_update(ReqModel).where(ReqModel.id == req_id).values(
                    raw_text=combined_raw, raw_images=all_raw_images,
                ))
                await s.commit()

            if await _is_cancelled():
                logger.info("[Task] 任务已取消（upload 后），提前退出: task_id={}", task_id)
                return
        elif not combined_raw:
            raise RuntimeError("无文件字节且无 combined_raw，无法执行需求分析")

        # ===== phase 1: parse（LLM 解析）=====
        await _update_task(task_id, status="running", phase="parse", progress=10,
                           message="正在调用 LLM 解析需求文档...")

        srr = await req_agent.parse(
            raw_requirement=combined_raw,
            product_id=product_id,
            product_name=product_name or "",
        )
        if not isinstance(srr, dict):
            raise RuntimeError(f"需求解析返回异常类型: {type(srr).__name__}")

        # P0-2: 立即捕获 FP，避免二次 parse 覆盖
        captured_fps = list(req_agent.last_fps)
        captured_fp_deps = list(req_agent.last_fp_dependencies)
        # v2.18: 捕获 interaction_srr，用于后续合并到总 SRR
        captured_interaction_srr = dict(req_agent.interaction_srr) if req_agent.interaction_srr else None

        if await _is_cancelled():
            logger.info("[Task] 任务已取消，提前退出: task_id={}", task_id)
            return

        req_data = srr.setdefault("requirement", {})
        # 覆盖调用方指定的字段
        if title:
            req_data["title"] = title
        if priority:
            req_data["priority"] = priority
        if module_list:
            req_data["modules"] = module_list

        await _update_task(task_id, phase="parse", progress=30, message="需求解析完成，正在检索知识库...")

        if await _is_cancelled():
            logger.info("[Task] 任务已取消（kb_search 前），提前退出: task_id={}", task_id)
            return

        # ===== phase 2: kb_search + clarify =====
        await _update_task(task_id, phase="kb_search", progress=40,
                           message="正在检索知识库历史需求并补充缺失字段...")

        clarify_result = None
        questions = []
        clarify_error: str | None = None
        async with async_session() as s:
            try:
                clarify_result = await clarify_agent.clarify(srr, session=s)
                questions = clarify_result.get("questions", [])
            except Exception as e:
                # v2.18: 关键错误不再静默——中断流水线，保存已解析结果，标记任务失败
                clarify_error = str(e)[:500]
                logger.error("[Task] ClarifyAgent 检测失败（关键错误，中断流水线）: {}", clarify_error)

        # v2.18: ClarifyAgent 失败时中断流水线，但保存已解析的基础 SRR+FP 供用户查看
        if clarify_error:
            base_srr = dict(srr)
            if captured_interaction_srr:
                base_srr = _merge_interaction_srr(base_srr, captured_interaction_srr)
            base_srr["_clarify_error"] = clarify_error
            async with async_session() as save_s:
                await save_s.execute(sa_update(ReqModel).where(ReqModel.id == req_id).values(
                    title=req_data.get("title", ""),
                    priority=req_data.get("priority", "P2"),
                    module=module_list[0] if module_list else "",
                    modules=module_list,
                    status="draft",
                    srr_json=base_srr,
                    raw_text=combined_raw,
                ))
                try:
                    await _save_fps_to_db(save_s, req_id, captured_fps, req_data.get("priority", "P2"))
                    await _save_fp_dependencies(save_s, req_id, captured_fp_deps)
                    await _update_fp_index_in_srr(save_s, req_id)
                except Exception as fp_e:
                    logger.error("[Task] ClarifyAgent 失败后基础 FP 保存也失败: {}", str(fp_e)[:200])
                await save_s.commit()

            err_msg = f"知识库检索/需求澄清失败: {clarify_error}"
            await _update_task(
                task_id, status="failed", phase="kb_search", error=err_msg,
                message="分析失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
            )
            logger.error("[Task] 需求分析因 ClarifyAgent 失败而中断: task_id={}, req_id={}", task_id, req_id)
            return

        # v2.24: 将 ClarifyAgent 子阶段耗时分解记录到 phase_timings["kb_search"]
        if clarify_result and clarify_result.get("phase_breakdown"):
            try:
                async with async_session() as s:
                    task = await s.get(AnalysisTask, task_id)
                    if task and task.phase_timings:
                        timings = dict(task.phase_timings)
                        kb_phase = dict(timings.get("kb_search", {}))
                        kb_phase["sub_breakdown"] = clarify_result["phase_breakdown"]
                        timings["kb_search"] = kb_phase
                        task.phase_timings = timings
                        await s.commit()
            except Exception as e:
                logger.warning("[Task] 写入 phase_breakdown 失败: {}", str(e)[:120])

        await _update_task(task_id, phase="clarify", progress=70,
                           message=f"知识库检索完成，检测到 {len(questions)} 个待补充问题" if questions else "知识库检索完成，正在生成最终分析...")

        # ===== 构建持久化数据 =====
        multi_augmented = clarify_result.get("augmented_srr", srr) if clarify_result else srr
        srr_json_data = dict(multi_augmented)
        if clarify_result:
            srr_json_data["_clarify_state"] = {
                "questions": questions,
                "auto_augmentations": clarify_result.get("auto_augmentations", []),
                "kb_status": clarify_result.get("kb_status", {}),
                "quality_report": clarify_result.get("quality_report", {}),
                "field_conflicts": clarify_result.get("field_conflicts", []),
            }
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                srr_json_data["_kb_query_logs"] = kb_logs

        needs_clarify = (clarify_result.get("status") == "needs_clarification") if clarify_result else (len(questions) > 0)
        # v1.6：分析完成后进入 pending_review（待确认），等待用户确认 SRR/FP/影响分析
        final_status = "draft" if needs_clarify else "pending_review"

        # ===== phase 3: regenerate（仅有 KB 补全信息时才重新解析）=====
        # v2.18: 修复 Bug — ClarifyAgent 失败或无 KB 补全时不应重新解析，
        # 避免覆盖第一次解析结果（第一次解析的 FP/interaction_srr 已捕获，二次解析会导致 SRR 与 FP 不一致）
        kb_augments = clarify_result.get("auto_augmentations", []) if clarify_result else []
        if not needs_clarify and kb_augments:
            await _update_task(task_id, phase="regenerate", progress=85,
                               message="正在融合 KB 补全信息，生成最终需求分析结果...")
            parts = [combined_raw, "\n\n## KB 自动补全信息\n"]
            for aug in kb_augments:
                parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")
            enhanced_raw = "\n".join(parts)
            try:
                final_srr = await req_agent.parse(
                    raw_requirement=enhanced_raw,
                    product_id=product_id or "PROD-DEFAULT",
                )
                final_srr_data = dict(final_srr)
                # 保留 KB 日志
                if clarify_result:
                    kb_logs = clarify_result.get("kb_access_log", [])
                    if kb_logs:
                        final_srr_data["_kb_query_logs"] = kb_logs
                final_srr_data.pop("_clarify_state", None)
                final_srr_data.pop("_field_sources", None)
                srr_json_data = final_srr_data
            except Exception as e:
                # v2.18: 降级时保留第一次解析结果，但记录降级原因供前端展示警告
                degrade_err = str(e)[:300]
                logger.error("[Task] 最终SRR生成失败→降级（保留首次解析结果）: {}", degrade_err)
                final_srr_data = dict(srr)
                final_srr_data["_finalize_degraded"] = True
                final_srr_data["_finalize_error"] = degrade_err
                srr_json_data = final_srr_data
        elif not needs_clarify:
            # ClarifyAgent 失败或无 KB 补全：保留第一次解析结果，仅清理临时字段
            srr_json_data.pop("_clarify_state", None)
            srr_json_data.pop("_field_sources", None)

        # ===== 持久化 Requirement（更新已创建的 analyzing 记录）=====
        # v2.18: 合并 interaction_srr 到总 SRR（统一所有流程的 interaction_srr 处理）
        if captured_interaction_srr:
            srr_json_data = _merge_interaction_srr(srr_json_data, captured_interaction_srr)

        async with async_session() as s:
            stmt = sa_update(ReqModel).where(ReqModel.id == req_id).values(
                title=req_data.get("title", ""),
                priority=req_data.get("priority", "P2"),
                module=module_list[0] if module_list else "",
                modules=module_list,
                status=final_status,
                srr_json=srr_json_data,
                raw_text=combined_raw,
            )
            await s.execute(stmt)
            await s.commit()

        # v1.5.1: 自动保存 ReqAgent 一体式提取的功能点 + 依赖关系
        # v2.18: FP 保存失败不再静默——中断流水线，标记任务失败（FP 是核心产物，0 FP 会导致后续流程全部失效）
        try:
            async with async_session() as s:
                await _save_fps_to_db(s, req_id, captured_fps, req_data.get("priority", "P2"))
                await _save_fp_dependencies(s, req_id, captured_fp_deps)
                await _update_fp_index_in_srr(s, req_id)  # v2.18: 更新总 SRR 的 fp_index
                await s.commit()
        except Exception as e:
            fp_err_msg = str(e)[:500]
            logger.error("[Task] FP 自动保存失败（关键错误，中断流水线）: {}", fp_err_msg)
            err_msg = f"功能点(FP)保存失败: {fp_err_msg}"
            await _update_task(
                task_id, status="failed", phase="done", error=err_msg,
                message="分析失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
            )
            return

        # ===== 完成 =====
        result = {
            "req_id": req_id,
            "title": req_data.get("title", ""),
            "status": final_status,
            "questions": questions,
            "has_unanswered_questions": needs_clarify,
            "source_files": source_files,
            "kb_status": clarify_result.get("kb_status", {}) if clarify_result else {},
            # v2.18: ClarifyAgent 失败时 kb_empty 设为 None（未知），不再误报为 True
            "kb_empty": clarify_result.get("kb_status", {}).get("kb_empty") if clarify_result else None,
        }
        # v2.18: 降级警告传递给前端
        if srr_json_data.get("_finalize_degraded"):
            result["degraded"] = True
            result["degraded_reason"] = srr_json_data.get("_finalize_error", "最终 SRR 生成失败，已降级使用首次解析结果")
        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message="分析完成" + ("，有待补充问题" if needs_clarify else ""),
            result_json=result, completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task] 需求分析完成: task_id={}, req_id={}, status={}, questions={}",
                    task_id, req_id, final_status, len(questions))

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task] 需求分析失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        await _update_task(
            task_id, status="failed", phase="", error=err_msg,
            message="分析失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
        )
        # 失败时将 Requirement 标记为 draft（便于用户看到记录）
        try:
            async with async_session() as s:
                stmt = sa_update(Requirement).where(Requirement.id == req_id).values(
                    status="draft", title=title or "(分析失败)",
                )
                await s.execute(stmt)
                await s.commit()
        except Exception as e:
            logger.warning("[API/requirements] 失败回退 Requirement 状态失败 req_id={}: {}", req_id, str(e)[:150])


# ============================================================================
# 1d. 多文件上传需求（v2.1 新增：需求文档 + 设计文档联合分析）
# ============================================================================

@router.post("/upload/multi", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement_multi(
    files: list[UploadFile] = File(..., description="多文件列表（需求文档 + 设计文档）"),
    product_id: str = Form(default="PROD-DEFAULT", description="产品 ID"),
    product_name: str = Form(default="", description="产品名称"),
    title: str = Form(default="", description="需求标题（可选）"),
    priority: str = Form(default="P2", description="优先级 P0-P3"),
    project_id: str = Form(default="", description="项目/版本 ID"),
    modules: str = Form(default="", description="关联模块（逗号分隔）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """上传多个需求文件（.md / .docx），DocMergeAgent 合并后统一分析。

    支持场景：
    - 上传需求文档 + 设计文档，联合分析互为上下文补充
    - 上传多个需求的片段，合并为一个完整需求

    支持格式：.md / .markdown / .docx
    文件上限：单个 10MB，总大小不超过 50MB

    v2.1 新增：
    - 多文档融合分析（DocMergeAgent）
    - 冲突检测与合并
    """
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not files or len(files) == 0:
        raise HTTPException(status_code=400, detail="请上传至少一个文件")

    MAX_TOTAL_SIZE = 50 * 1024 * 1024
    total_size = 0

    # v2.24: 先读取文件字节（快速操作），文件解析移到后台协程中
    # 这样任务记录可以在文件解析之前就创建，用户立即看到任务
    file_bytes_list: list[tuple[str, bytes]] = []
    for file in files:
        filename = file.filename or ""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext not in ("md", "markdown", "docx", "txt"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: .{ext}（文件：{filename}），仅支持 .md / .docx / .txt",
            )
        raw_bytes = await file.read()
        total_size += len(raw_bytes)
        if total_size > MAX_TOTAL_SIZE:
            raise HTTPException(status_code=400, detail="所有文件总大小超过 50MB 上限")
        file_bytes_list.append((filename, raw_bytes))

    # v2.24: 立即创建 Requirement + AnalysisTask（phase=upload），文件解析在协程中完成
    req_id = gen_requirement_id()
    task_id = gen_task_id()

    record = Requirement(
        id=req_id,
        product_id=product_id,
        project_id=project_id or None,
        title=title or "(分析中...)",
        priority=priority,
        module=module_list[0] if module_list else "",
        modules=module_list,
        tags=[],
        version=1,
        status="analyzing",
        srr_json={},
        raw_text="",
        raw_images=[],
        source_type="multi",
        created_by="api",
    )
    session.add(record)
    await session.flush()

    task = AnalysisTask(
        id=task_id,
        task_type="upload_requirement",
        req_id=req_id,
        product_id=product_id,
        status="pending",
        phase="upload",
        progress=0,
        message="已接收文件，准备解析...",
        created_by="api",
        # v2.24: 初始化 upload 阶段的 phase_timings
        phase_timings={"upload": {"start": datetime.now(timezone.utc).isoformat()}},
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）— 文件字节传递给协程解析
    asyncio.create_task(_run_requirement_analysis(
        task_id=task_id,
        req_id=req_id,
        combined_raw="",  # v2.24: 文件解析移到协程中，初始为空
        product_id=product_id,
        product_name=product_name,
        title=title,
        priority=priority,
        project_id=project_id,
        module_list=module_list,
        source_files=[f[0] for f in file_bytes_list],
        file_bytes_list=file_bytes_list,  # v2.24: 传递原始文件字节
    ))

    logger.info("[API] 多文档上传已派发异步分析: task_id={}, req_id={}, files={}",
                task_id, req_id, [f[0] for f in file_bytes_list])

    return ApiResponse(
        success=True,
        message=f"已接收 {len(file_bytes_list)} 个文件，正在后台分析",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "source_files": [f[0] for f in file_bytes_list],
        },
    )


# ============================================================================
# 1e. 任务状态查询（v2.14 新增 - 供前端轮询分析进度）
# ============================================================================

@router.get("/tasks", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选，支持逗号分隔多值（如 running,pending）"),
    task_type: str | None = Query(default=None, description="按任务类型筛选 upload_requirement/submit_clarify/..."),
    product_id: str | None = Query(default=None, description="按产品筛选"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出分析任务（支持按状态/类型/产品筛选，分页）。

    v2.20: status 参数支持逗号分隔多值（如 ``running,pending``），
    便于前端查询"所有未结束任务"。
    """
    conditions = []
    if status:
        status_list = [s.strip() for s in status.split(",") if s.strip()]
        if len(status_list) == 1:
            conditions.append(AnalysisTask.status == status_list[0])
        elif status_list:
            conditions.append(AnalysisTask.status.in_(status_list))
    if task_type:
        conditions.append(AnalysisTask.task_type == task_type)
    if product_id:
        conditions.append(AnalysisTask.product_id == product_id)

    count_stmt = select(func.count(AnalysisTask.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = select(AnalysisTask).order_by(AnalysisTask.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    # 关联需求标题（避免 N+1，批量查询）
    req_ids = {r.req_id for r in rows if r.req_id}
    req_titles: dict[str, str] = {}
    if req_ids:
        title_rows = await session.execute(
            select(Requirement.id, Requirement.title).where(Requirement.id.in_(req_ids))
        )
        req_titles = dict(title_rows.all())

    items = []
    for r in rows:
        d = r.to_dict()
        d["req_title"] = req_titles.get(r.req_id or "", "")
        items.append(d)

    # v2.36: 携带服务器当前时间，避免前端计算耗时出现负数
    return ApiResponse(success=True, message="ok", data={
        "items": items,
        "total": total,
        "server_now": datetime.now(timezone.utc).isoformat(),
    })


@router.get("/tasks/{task_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查询单个任务状态（供前端轮询进度）。"""
    task = await session.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    d = task.to_dict()
    # v2.36: 携带服务器当前时间，避免前端用 Date.now() 计算耗时
    # 时因客户端时钟与服务器不同步导致负数
    d["server_now"] = datetime.now(timezone.utc).isoformat()
    # 关联需求标题
    if task.req_id:
        req = await session.get(Requirement, task.req_id)
        d["req_title"] = req.title if req else ""
    else:
        d["req_title"] = ""

    return ApiResponse(success=True, message="ok", data=d)


@router.get("/{req_id}/task", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_task_by_req(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """按需求 ID 查询关联的最新任务（供需求详情页恢复进度）。"""
    stmt = (
        select(AnalysisTask)
        .where(AnalysisTask.req_id == req_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    task = result.scalar_one_or_none()

    if not task:
        return ApiResponse(success=True, message="无关联任务", data=None)

    d = task.to_dict()
    # v2.36: 携带服务器当前时间，避免前端计算耗时出现负数
    d["server_now"] = datetime.now(timezone.utc).isoformat()
    return ApiResponse(success=True, message="ok", data=d)


@router.post("/tasks/{task_id}/cancel", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def cancel_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """取消分析任务（将 running/pending 标记为 interrupted）。

    Note:
        - 无法真正终止正在运行的 asyncio 协程，仅标记状态。
          后台协程在下次更新状态时会检测到 interrupted 并提前退出。
        - 已完成/已失败/已中断的任务不可取消。

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 409: 任务不在可取消状态
    """
    task = await session.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status}，无法取消（仅 pending/running 可取消）",
        )

    task.status = "interrupted"
    task.error = "用户手动取消"
    task.message = "已取消"
    task.completed_at = datetime.now(timezone.utc)

    # v2.23: 记录当前阶段的结束时间到 phase_timings
    if task.phase and task.phase_timings:
        timings = dict(task.phase_timings)
        phase_data = dict(timings.get(task.phase, {}))
        if "end" not in phase_data and "start" in phase_data:
            try:
                start_dt = datetime.fromisoformat(phase_data["start"])
                phase_data["end"] = task.completed_at.isoformat()
                phase_data["duration_ms"] = int((task.completed_at - start_dt).total_seconds() * 1000)
                timings[task.phase] = phase_data
                task.phase_timings = timings
            except (ValueError, TypeError):
                pass

    # 若关联需求仍为 analyzing 状态，回退为 draft 避免卡死
    if task.req_id:
        req = await session.get(Requirement, task.req_id)
        if req and req.status == "analyzing":
            req.status = "draft"
            logger.info("[API] 任务取消，需求状态回退: req_id={} analyzing→draft", task.req_id)

    await session.commit()
    logger.info("[API] 任务已取消: task_id={}, task_type={}", task_id, task.task_type)

    return ApiResponse(success=True, message="任务已取消", data=task.to_dict())


# ============================================================================
# 2. 需求列表（分页）
# ============================================================================

@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_requirements(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    project_id: str | None = Query(default=None, description="按项目 ID 筛选"),
    priority: str | None = Query(default=None, description="按优先级筛选 P0-P3"),
    status: str | None = Query(default=None, description="按状态筛选 draft/active/archived"),
    search: str | None = Query(default=None, description="按标题模糊搜索"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=300, description="每页条数，最大 300"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求列表（分页）。

    Args:
        product_id: 按产品 ID 筛选（可选）
        project_id: 按项目/版本 ID 筛选（可选）
        priority: 按优先级筛选（可选）
        status: 按状态筛选（可选）
        search: 按标题模糊搜索（可选）
        page: 页码，从 1 开始
        page_size: 每页条数
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items（RequirementSummary 列表）/ total / page / page_size
    """
    conditions = []
    if product_id:
        conditions.append(Requirement.product_id == product_id)
    if project_id:
        if project_id == "unassigned":
            # 筛选未绑定项目的需求（project_id IS NULL）
            conditions.append(Requirement.project_id.is_(None))
        else:
            conditions.append(Requirement.project_id == project_id)
    if priority:
        conditions.append(Requirement.priority == priority)
    if status:
        conditions.append(Requirement.status == status)
    if search:
        conditions.append(Requirement.title.ilike(f"%{search}%"))

    # 计算总数
    count_stmt = select(func.count(Requirement.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await session.execute(count_stmt)).scalar() or 0

    # 分页查询（按创建时间倒序）
    stmt = select(Requirement).order_by(Requirement.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        RequirementSummary(
            id=r.id,
            title=r.title,
            product_id=r.product_id,
            project_id=r.project_id or "",
            priority=r.priority,
            status=r.status,
            source_type=r.source_type,
            modules=r.modules or [],
            tags=r.tags or [],
            created_at=r.created_at.isoformat() if r.created_at else "",
        ).model_dump()
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
# 3. 需求详情
# ============================================================================

@router.get("/{req_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求详情（含完整 SRR JSON）。

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含需求元数据 + srr_json + raw_text

    Raises:
        HTTPException 404: 需求不存在
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "id": record.id,
            "product_id": record.product_id,
            "title": record.title,
            "priority": record.priority,
            "modules": record.modules,
            "status": record.status,
            "version": record.version,
            "srr_json": record.srr_json,  # 注意字段名 srr_json（前端和类型定义使用此名）
            "raw_text": record.raw_text,
            "raw_images": record.raw_images or [],  # v2.18: docx 原始图片（base64 data_url），供前端展示对比
            "supplementary_text": record.supplementary_text or "",  # v2.17: 补充文件内容
            "source_type": record.source_type,
            "kb_query_logs": (record.srr_json or {}).get("_kb_query_logs", []),
            "questions_count": len(((record.srr_json or {}).get("_clarify_state", {}) or {}).get("questions", [])),
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "updated_at": record.updated_at.isoformat() if record.updated_at else "",
            # ===== 修复问题 3：补充向量化与归档相关字段 =====
            "vectorization_status": record.vectorization_status,  # null/processing/completed/failed
            "vectorization_error": record.vectorization_error or "",
            "go_live_date": record.go_live_date.isoformat() if record.go_live_date else "",
            "archived_at": record.archived_at.isoformat() if record.archived_at else "",
            "revision": record.revision or 1,  # v2.33: 多人协作冲突检测
        },
    )


@router.get("/{req_id}/revision", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement_revision(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """v2.33: 轻量端点——返回需求的 revision 和任务状态，供前端轮询检测多人协作冲突。

    前端每 5 秒轮询此端点，对比 revision 变化时显示"需求已被他人修改，请刷新"提示条。
    同时返回是否有运行中的分析任务，用于显示"正在执行分析任务"提示。

    Returns:
        ApiResponse: data 含 revision / updated_at / task_running / task_message
    """
    result = await session.execute(
        select(Requirement.revision, Requirement.updated_at, Requirement.status).where(Requirement.id == req_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    revision = row[0] or 1
    updated_at = row[1].isoformat() if row[1] else ""
    req_status = row[2]

    # 检查是否有运行中的分析任务
    task_running = False
    task_message = ""
    task_type_label = ""
    if req_status == "analyzing":
        task_running = True
        task_message = "正在分析需求..."
    else:
        task_result = await session.execute(
            select(AnalysisTask).where(
                AnalysisTask.req_id == req_id,
                AnalysisTask.status.in_(["pending", "running"]),
            ).order_by(AnalysisTask.created_at.desc()).limit(1)
        )
        running_task = task_result.scalar_one_or_none()
        if running_task:
            task_running = True
            labels = {
                "upload_requirement": "需求解析",
                "regenerate": "重新生成",
                "merge_fp": "功能点合并",
                "submit_clarify": "澄清重解析",
            }
            task_type_label = labels.get(running_task.task_type, running_task.task_type)
            task_message = running_task.message or f"正在执行{task_type_label}任务..."

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "revision": revision,
            "updated_at": updated_at,
            "status": req_status,
            "task_running": task_running,
            "task_type": task_type_label,
            "task_message": task_message,
        },
    )


# ============================================================================
# 4. 获取澄清问题
# ============================================================================

@router.get("/{req_id}/clarify/questions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_clarify_questions(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求的待回答澄清问题。

    调用 ClarifyAgent 对 SRR 执行澄清分析，返回需要人工确认的问题列表。
    本端点不修改数据库，仅返回问题供前端展示。

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 status / questions / quality_report / kb_status

    Raises:
        HTTPException 404: 需求不存在
    """
    # 从 DB 取 SRR
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    srr = record.srr_json or {}
    if not srr:
        raise HTTPException(status_code=400, detail="需求 SRR 为空，无法澄清")

    logger.info("[API/requirements] 获取澄清问题: req_id={}", req_id)

    # 读取上传时保存的澄清状态（快照：暂不处理后回来看到的应是同一份问题，不重新生成）
    clarify_state = srr.get("_clarify_state", {}) or {}
    saved_questions = clarify_state.get("questions", []) or []
    saved_answers = srr.get("clarify_answers", {}) or {}

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "req_id": req_id,
            "status": "needs_clarification" if saved_questions else "passed",
            "questions": saved_questions,
            "previous_answers": saved_answers,
            "quality_report": clarify_state.get("quality_report", {}),
            "kb_status": clarify_state.get("kb_status", {}),
            "auto_augmentations": clarify_state.get("auto_augmentations", []),
            # v2.12: 字段冲突列表（docs/01 §4.4.1），供前端展示人工确认
            "field_conflicts": clarify_state.get("field_conflicts", []),
        },
    )


# ============================================================================
# 5. 提交澄清答案
# ============================================================================

@router.post("/{req_id}/clarify", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def submit_clarify_answers(
    req_id: str,
    req: ClarifyAnswerRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """提交澄清问题答案 → 异步重新生成最终 SRR。

    v2.15 异步化（变更组 C）：
        1. 同步：验证答案 + 构建增强文本 + 保存澄清历史快照
        2. 创建 AnalysisTask（task_type=submit_clarify）
        3. 启动后台协程 _run_clarify_reparse（执行 LLM parse + FP 重提取）
        4. 立即返回 task_id，前端轮询 GET /tasks/{task_id} 获取进度

    Args:
        req_id: 需求 ID
        req: 澄清答案请求，含 answers 字典 {question_id: answer}
        session: 数据库会话

    Returns:
        ApiResponse: data 含 task_id / req_id / status=analyzing
    """
    if not req.answers:
        raise HTTPException(status_code=400, detail="澄清答案不能为空")

    # v2.33: 互斥锁检查——防止与其他分析任务并发冲突
    await _check_no_running_task(req_id)

    # 从 DB 取需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status not in ("draft", "pending_review", "confirmed"):
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {record.status} 不可提交澄清（仅 draft/pending_review/confirmed 可提交）",
        )
    # 重新生成时重置状态为 draft（等待重新分析完成后自动进入 pending_review）
    if record.status == "confirmed":
        record.status = "draft"

    raw_text = record.raw_text or ""
    srr_json = dict(record.srr_json or {})
    clarify_state = srr_json.get("_clarify_state", {}) or {}
    questions = clarify_state.get("questions", []) or []

    # v2.29: 若 _clarify_state 已被清空（重新解析后会 pop），从最近的分析任务
    # result_json 中找回澄清问题，确保澄清历史能完整记录 Q&A 对。
    if not questions:
        try:
            recent_task_stmt = (
                select(AnalysisTask.result_json)
                .where(AnalysisTask.req_id == req_id)
                .where(AnalysisTask.task_type == "upload_requirement")
                .order_by(AnalysisTask.created_at.desc())
                .limit(1)
            )
            rt = await session.execute(recent_task_stmt)
            rt_row = rt.scalar_one_or_none()
            if rt_row:
                rt_data = rt_row if isinstance(rt_row, dict) else json.loads(rt_row)
                task_questions = rt_data.get("questions", []) if isinstance(rt_data, dict) else []
                if task_questions:
                    questions = task_questions
                    logger.info("[API/requirements] 从 task result_json 回找澄清问题: count={}", len(questions))
        except Exception as e:
            logger.warning("[API/requirements] 回找澄清问题失败: {}", str(e)[:200])

    # ===== ⓪ 保存澄清历史快照（在 re-parse 前留存 Q&A 记录） =====
    # v2.29: answers 保存为结构化 Q&A 列表（含 question 文本），而非裸 id→answer 映射，
    # 确保即使后续 _clarify_state 被清空，历史记录仍能独立展示完整 Q&A。
    qa_records: list[dict] = []
    for q in questions:
        qid = q.get("id", "") if isinstance(q, dict) else ""
        ans_val = req.answers.get(qid, "").strip() if isinstance(req.answers, dict) else ""
        if qid:
            qa_records.append({
                "id": qid,
                "field": q.get("field", "") if isinstance(q, dict) else "",
                "question": q.get("question", "") if isinstance(q, dict) else "",
                "priority": q.get("priority", "") if isinstance(q, dict) else "",
                "answer": ans_val,
            })

    history_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "clarify",
        "questions": questions,
        "answers": dict(req.answers),
        "qa_records": qa_records,
        "extra_notes": req.extra_notes or "",
        "auto_augmentations": clarify_state.get("auto_augmentations", []),
        "field_conflicts": clarify_state.get("field_conflicts", []),
        "quality_report": clarify_state.get("quality_report", {}),
    }
    clarify_history = list(srr_json.get("_clarify_history", []))
    clarify_history.append(history_entry)

    # ===== ① 构建增强后的需求文本（原始文本 + Q&A 补充 + 额外说明） =====
    enhanced_parts = [raw_text]

    # 将 Q&A 对格式化为结构化补充信息
    qa_pairs = []
    for q in questions:
        qid = q.get("id", "")
        answer = req.answers.get(qid, "").strip()
        if answer:
            question_text = q.get("question", "")
            field = q.get("field", "")
            qa_pairs.append(f"- 【{field}】{question_text}\n  回答：{answer}")

    # v2.12: 处理字段冲突解决答案（conflict_<field> 键）
    field_conflicts = clarify_state.get("field_conflicts", []) or []
    for c in field_conflicts:
        c_field = c.get("field", "")
        conflict_answer = req.answers.get(f"conflict_{c_field}", "").strip()
        if conflict_answer:
            existing_src = c.get("existing_source", "")
            suggested_src = c.get("suggested_source", "")
            qa_pairs.append(
                f"- 【{c_field}】字段冲突解决（原文来源:{existing_src} / KB建议来源:{suggested_src}）\n"
                f"  最终采用值：{conflict_answer}"
            )

    if qa_pairs:
        enhanced_parts.append("\n\n## 补充信息（用户澄清）\n")
        enhanced_parts.extend(qa_pairs)

    # 合并 KB 已自动补全的字段（避免人工澄清时丢失 KB 已查到的答案）
    kb_augments = (srr_json.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        enhanced_parts.append("\n\n## KB 自动补全信息\n")
        for aug in kb_augments:
            val = aug.get("value", "")
            enhanced_parts.append(f"- {aug.get('field', '')}: {str(val)[:300]}")

    # 额外补充说明
    if req.extra_notes and req.extra_notes.strip():
        enhanced_parts.append(f"\n\n## 额外补充说明\n{req.extra_notes.strip()}")

    enhanced_raw = "\n".join(enhanced_parts)

    logger.info(
        "[API/requirements] 提交澄清答案并启动异步重新生成: req_id={}, answers={}, enhanced_len={}",
        req_id,
        len(req.answers),
        len(enhanced_raw),
    )

    # v2.15: 标记需求为 analyzing 状态
    record.status = "analyzing"
    await session.commit()

    # ===== ② 创建 AnalysisTask + 启动后台协程 =====
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="submit_clarify",
        req_id=req_id,
        product_id=record.product_id,
        status="pending",
        phase="parse",
        progress=0,
        message="已接收澄清答案，准备重新生成...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）
    asyncio.create_task(_run_clarify_reparse(
        task_id=task_id,
        req_id=req_id,
        enhanced_raw=enhanced_raw,
        original_raw=record.raw_text or "",  # v1.6: 传入原始文本用于 FP 提取，避免增强文本导致 FP 偏差
        product_id=record.product_id,
        clarify_history=clarify_history,
        old_srr=srr_json,
        priority=record.priority,
    ))

    return ApiResponse(
        success=True,
        message="已接收澄清答案，正在后台重新生成",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "answers_count": len(req.answers),
        },
    )


async def _run_clarify_reparse(
    task_id: str,
    req_id: str,
    enhanced_raw: str,
    original_raw: str,  # v1.6: 原始需求文本（用于 FP 提取，避免增强文本导致 FP 偏差）
    product_id: str,
    clarify_history: list,
    old_srr: dict,
    priority: str,
) -> None:
    """后台执行澄清后的 SRR 重新生成（异步协程，独立 session）。

    流程：
        1. phase=parse: req_agent.parse() 重新生成 SRR（含降级处理）
        2. 持久化 SRR + 保留 KB 日志 + 迁移 _baseline_context
        3. FP 重提取（使用 original_raw 而非 enhanced_raw，确保与需求分析时一致）
        4. status=completed
    """
    from sqlalchemy import update as sa_update, delete as sa_del
    try:
        await _update_task(task_id, status="running", phase="parse", progress=20,
                           message="正在调用 LLM 重新生成 SRR...")

        # ===== 重新调用 ReqAgent 生成最终 SRR =====
        try:
            final_srr = await req_agent.parse(
                raw_requirement=enhanced_raw,
                product_id=product_id,
            )
            logger.info("[Task/Clarify] 最终 SRR 生成成功: req_id={}", req_id)
        except Exception as e:
            logger.error("[Task/Clarify] 最终 SRR 生成失败 → 降级: {}", str(e)[:200])
            # 降级：使用原有 SRR
            final_srr = dict(old_srr)
            final_srr.pop("_clarify_state", None)
            final_srr.pop("clarify_answers", None)
            final_srr.pop("_field_sources", None)
            final_srr["_clarify_degraded"] = True

        # P0-2: 立即捕获 FP，避免后续操作覆盖
        # v1.6: 使用 original_raw（原始需求文本）进行 FP 提取，而非 enhanced_raw。
        # 增强文本包含 KB 补全和澄清答案，会导致 LLM 提取更多/不同的功能点，
        # 与需求分析时提取的 FP 不一致。使用原始文本确保 FP 提取结果稳定一致。
        if original_raw:
            try:
                captured_fps = await req_agent._fp_extract_from_raw(
                    raw_text=original_raw,
                    srr=final_srr,
                )
                captured_fp_deps = list(req_agent.last_fp_dependencies)
                # v2.18: 捕获 interaction_srr，用于后续合并到总 SRR
                captured_interaction_srr = dict(req_agent.interaction_srr) if req_agent.interaction_srr else None
                logger.info("[Task/Clarify] FP 重提取完成（基于原始文本）: {} 个", len(captured_fps))
            except Exception as fp_e:
                logger.warning("[Task/Clarify] FP 重提取失败（降级使用 enhanced_raw 结果）: {}", str(fp_e)[:200])
                captured_fps = list(req_agent.last_fps)
                captured_fp_deps = list(req_agent.last_fp_dependencies)
                captured_interaction_srr = dict(req_agent.interaction_srr) if req_agent.interaction_srr else None
        else:
            captured_fps = list(req_agent.last_fps)
            captured_fp_deps = list(req_agent.last_fp_dependencies)
            captured_interaction_srr = dict(req_agent.interaction_srr) if req_agent.interaction_srr else None

        await _update_task(task_id, phase="parse", progress=60,
                           message="SRR 生成完成，正在保存功能点...")

        # ===== 持久化 =====
        final_data = dict(final_srr)
        final_data.pop("_clarify_state", None)
        final_data.pop("clarify_answers", None)
        final_data.pop("_field_sources", None)

        # 保留 KB 查询日志
        if original_kb_logs := old_srr.get("_kb_query_logs"):
            final_data["_kb_query_logs"] = original_kb_logs
        # 保留澄清历史
        final_data["_clarify_history"] = clarify_history
        # 合并旧 SRR 的 KB 基线补全内容（v2.40: 移除 additional_details，仅保留 _baseline_context）
        old_baseline = old_srr.get("_baseline_context")
        if old_baseline:
            final_data["_baseline_context"] = old_baseline

        # v2.18: 合并 interaction_srr 到总 SRR（统一所有流程的 interaction_srr 处理）
        if captured_interaction_srr:
            final_data = _merge_interaction_srr(final_data, captured_interaction_srr)

        # v2.32/v2.34: 从澄清答案中提取历史数据影响信息，写入 SRR.legacy_data_impact
        # v2.34 扩展：除 user_clarification 文本框外，还提取多选题 kb_optimization_impact
        # 以及 v2.34 新增的 legacy_data_impact.* 专项字段（migration_strategy/rollback_strategy 等）
        try:
            if clarify_history:
                latest_clarify = clarify_history[-1] if isinstance(clarify_history[-1], dict) else {}
                latest_answers = latest_clarify.get("answers", {}) or {}
                ldi = final_data.get("legacy_data_impact")
                if not isinstance(ldi, dict):
                    ldi = {}
                updated_fields: list[str] = []

                # 1. 多选题 kb_optimization_impact：用户选择的影响类型
                #    映射到 impact_scope 字段（逗号分隔），并据此推断 data_inventory 等
                impact_types = latest_answers.get("kb_optimization_impact", "")
                if isinstance(impact_types, list):
                    impact_types = ",".join(str(x) for x in impact_types if x)
                impact_types = str(impact_types or "").strip()
                if impact_types:
                    # 若 LLM 已生成 impact_scope，追加用户选择；否则用用户选择初始化
                    existing_scope = str(ldi.get("impact_scope", "") or "").strip()
                    if existing_scope and impact_types not in existing_scope:
                        ldi["impact_scope"] = f"{existing_scope}; 用户选择: {impact_types}"
                    elif not existing_scope:
                        ldi["impact_scope"] = impact_types
                    updated_fields.append("impact_scope")

                # 2. 文本框 kb_optimization_impact_scope：用户填写的具体影响范围
                user_impact_scope = str(latest_answers.get("kb_optimization_impact_scope", "")).strip()
                if user_impact_scope:
                    ldi["user_clarification"] = user_impact_scope
                    updated_fields.append("user_clarification")

                # 3. v2.34 新增：legacy_data_impact.* 专项字段（来自 LLM 生成的问题）
                #    问题 id 规则：kb_optimization_<subfield> 或 legacy_data_impact.<subfield>
                LDI_SUBFIELDS = (
                    "migration_strategy",
                    "rollback_strategy",
                    "compatibility_analysis",
                    "verification_method",
                    "impact_scope",
                )
                for sub in LDI_SUBFIELDS:
                    # 支持两种 question id 格式
                    for qid in (f"kb_optimization_{sub}", f"legacy_data_impact.{sub}"):
                        ans = latest_answers.get(qid)
                        if ans is None:
                            continue
                        # 多选/单选答案可能是 list，文本框是 str
                        if isinstance(ans, list):
                            ans = ",".join(str(x) for x in ans if x)
                        ans = str(ans or "").strip()
                        if not ans:
                            continue
                        # 不覆盖用户已填写的 user_clarification
                        if sub == "impact_scope" and ans == impact_types:
                            continue  # 已在步骤 1 处理
                        existing = str(ldi.get(sub, "") or "").strip()
                        if existing and ans not in existing:
                            ldi[sub] = f"{existing}; {ans}"
                        elif not existing:
                            ldi[sub] = ans
                        updated_fields.append(sub)

                if updated_fields:
                    # 若 LLM 未标记 is_optimization，用户回答了影响问题则标记为 True
                    ldi.setdefault("is_optimization", True)
                    final_data["legacy_data_impact"] = ldi
                    logger.info(
                        "[Task/Clarify] 写入 legacy_data_impact: req_id={}, fields={}",
                        req_id, updated_fields,
                    )
        except Exception as ldi_e:
            logger.warning("[Task/Clarify] 写入 legacy_data_impact 失败(不阻断): {}", str(ldi_e)[:200])

        async with async_session() as s:
            await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(
                srr_json=final_data, status="pending_review",
                # v2.33: 自增 revision（澄清重解析属于内容变更）
                revision=Requirement.revision + 1,
            ))
            await s.commit()

        logger.info(
            "[Task/Clarify] 澄清重解析完成，需求进入待确认: req_id={}, title={}",
            req_id,
            final_data.get("requirement", {}).get("title", ""),
        )

        # ===== FP 重提取（含 P3 修复：清理旧 FP 向量）=====
        if captured_fps:
            async with async_session() as s:
                old_fps = await s.execute(
                    select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
                )
                old_ids = {r[0] for r in old_fps.fetchall()}
                if old_ids:
                    await s.execute(
                        sa_del(FPDependency).where(
                            (FPDependency.source_fp_id.in_(old_ids)) | (FPDependency.target_fp_id.in_(old_ids))
                        )
                    )
                    await s.execute(
                        sa_del(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
                    )
                await s.flush()

                # P3 修复：删除旧 FP 对应的 Milvus 向量
                if old_ids:
                    try:
                        from app.db.milvus_store import vector_store
                        for old_fp_id in old_ids:
                            try:
                                await asyncio.to_thread(vector_store.delete_requirement_from_kb, f"FP-{old_fp_id}")
                            except Exception as fp_err:
                                logger.warning("[Task/Clarify] 旧 FP 向量删除失败(不阻断): fp_id={}, error={}", old_fp_id, str(fp_err)[:100])
                        logger.info("[Task/Clarify] 已清理 {} 个旧 FP 向量", len(old_ids))
                    except Exception as e:
                        logger.warning("[Task/Clarify] 旧 FP 向量清理失败(不阻断): {}", str(e)[:200])

                await _save_fps_to_db(s, req_id, captured_fps, priority)
                await _save_fp_dependencies(s, req_id, captured_fp_deps)
                await _update_fp_index_in_srr(s, req_id)  # v2.18: 更新总 SRR 的 fp_index
                await s.commit()
                logger.info("[Task/Clarify] 澄清后重新提取 FP: {} 个", len(captured_fps))

        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message="澄清完成，需求进入待确认",
            result_json={"req_id": req_id, "status": "pending_review"},
            completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task/Clarify] 澄清重生成完成: task_id={}, req_id={}", task_id, req_id)

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task/Clarify] 澄清重生成失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        await _update_task(
            task_id, status="failed", error=err_msg,
            message="澄清重生成失败: " + err_msg[:200],
            completed_at=datetime.now(timezone.utc),
        )
        # 失败时将需求状态回退为 draft，避免卡在 analyzing
        try:
            async with async_session() as s:
                await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(status="draft"))
                await s.commit()
        except Exception as revert_err:
            logger.warning("[Task/Clarify] 失败回退状态失败: req_id={}, error={}", req_id, str(revert_err)[:150])


# ============================================================================
# 5b. 确认生成最终 SRR（v2.7：KB 已解答时跳过人工，直接生成）
# ============================================================================

@router.post("/{req_id}/finalize", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def finalize_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """确认需求分析结果并生成最终 SRR。

    KB 已解答全部问题 → 将 KB 补全信息融合进原始需求文本 → ReqAgent 重解析 → 生成最终 SRR。
    无需人工介入，直接标记为 active。

    Args:
        req_id: 需求 ID
        session: 数据库会话
    """
    from app.agents.req_agent import req_agent

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")
    if record.status != "draft":
        raise HTTPException(status_code=400, detail="只能对 draft 状态的需求执行此操作")

    srr = record.srr_json or {}
    raw_text = record.raw_text or ""

    # 收集 KB 已解答的补全信息
    kb_augments = (srr.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        parts = [raw_text, "\n\n## KB 自动补全信息\n"]
        for aug in kb_augments:
            parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")
        enhanced_raw = "\n".join(parts)
    else:
        enhanced_raw = raw_text

    logger.info("[API/requirements] 最终 SRR 生成: req_id={}, kb_augments={}", req_id, len(kb_augments))

    try:
        final_srr = await req_agent.parse(
            raw_requirement=enhanced_raw,
            product_id=record.product_id,
        )
    except Exception as e:
        logger.error("[API/requirements] 最终 SRR 生成失败: {}", str(e)[:200])
        # 降级：使用原始 SRR
        final_srr = dict(srr)
        final_srr.pop("_clarify_state", None)
        final_srr.pop("clarify_answers", None)
        final_srr.pop("_field_sources", None)
        final_srr["_finalize_degraded"] = True

    # 保留 KB 日志
    final_data = dict(final_srr)
    if original_logs := (dict(record.srr_json or {}).get("_kb_query_logs")):
        final_data["_kb_query_logs"] = original_logs
    # 保留澄清历史（finalize 也可能在已有历史的需求上执行）
    if existing_history := (dict(record.srr_json or {}).get("_clarify_history")):
        final_data["_clarify_history"] = existing_history
    # 合并旧 SRR 的 KB 基线补全内容
    old_srr = dict(record.srr_json or {})
    # v2.40: 移除 additional_details，仅保留 _baseline_context
    old_baseline = old_srr.get("_baseline_context")
    if old_baseline:
        final_data["_baseline_context"] = old_baseline
    final_data.pop("_clarify_state", None)
    final_data.pop("clarify_answers", None)
    final_data.pop("_field_sources", None)

    record.srr_json = final_data
    record.status = "active"
    await session.commit()

    logger.info("[API/requirements] KB 确认完成: req_id={}, title={}", req_id, record.title)

    return ApiResponse(
        success=True,
        message="KB 已解答全部问题，最终 SRR 已生成",
        data={"req_id": req_id, "status": "active", "srr": final_data},
    )


# ============================================================================
# 5c. 澄清历史 + 重新生成
# ============================================================================

@router.get("/{req_id}/clarify/history", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_clarify_history(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求的澄清与重新生成历史。

    返回 _clarify_history 数组（按时间正序），每条记录含 timestamp / action /
    questions / answers / extra_notes / auto_augmentations / quality_report 等。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    history = list((record.srr_json or {}).get("_clarify_history", []))
    history.reverse()  # 前端展示倒序（最新在前）

    return ApiResponse(
        success=True,
        message="ok",
        data={"items": history, "total": len(history)},
    )


# ============================================================================
# v2.0 需求历史版本管理：列出/查看历次重新分析前的 SRR 快照
# ============================================================================

@router.get("/{req_id}/versions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_requirement_versions(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出需求的历史版本（重新分析前的 SRR 快照）。

    历史版本按时间倒序返回（最新快照在前）。每条记录含：
        - version_index: 版本序号（从 1 开始递增）
        - snapshot_at: 快照时间（ISO8601）
        - title: 当时的需求标题
        - trigger: 触发原因（regenerate / clarify 等）
        - extra_notes: 用户补充说明
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    versions = list((record.srr_json or {}).get("_version_history", []))
    # 列表视图：剥离 srr 大字段，仅返回摘要
    summary = [{
        "version_index": v.get("version_index", idx + 1),
        "snapshot_at": v.get("snapshot_at", ""),
        "title": v.get("title", ""),
        "trigger": v.get("trigger", ""),
        "extra_notes": (v.get("extra_notes", "") or "")[:200],
    } for idx, v in enumerate(versions)]
    summary.reverse()  # 最新在前

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "items": summary,
            "total": len(summary),
            "current_title": record.title,
            "current_status": record.status,
            "current_updated_at": record.updated_at.isoformat() if record.updated_at else "",
        },
    )


@router.get("/{req_id}/versions/{version_index}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement_version(
    req_id: str,
    version_index: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查看指定历史版本的完整 SRR 快照。

    Args:
        req_id: 需求 ID
        version_index: 版本序号（从 1 开始）
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    versions = list((record.srr_json or {}).get("_version_history", []))
    target = next((v for v in versions if v.get("version_index") == version_index), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version_index} 不存在（共 {len(versions)} 个历史版本）")

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "version_index": target.get("version_index"),
            "snapshot_at": target.get("snapshot_at", ""),
            "title": target.get("title", ""),
            "trigger": target.get("trigger", ""),
            "extra_notes": target.get("extra_notes", ""),
            "srr": target.get("srr", {}),
        },
    )


@router.post("/{req_id}/versions/{version_index}/restore", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def restore_requirement_version(
    req_id: str,
    version_index: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """切换使用指定历史版本（用版本快照替换当前 SRR）。

    当前 SRR 会先作为新版本快照保存，然后再被目标版本覆盖。
    v2.2 新增，大哥要求历史版本可直接切换使用。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    old_srr = dict(record.srr_json or {})
    versions = list(old_srr.get("_version_history", []))
    target = next((v for v in versions if v.get("version_index") == version_index), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version_index} 不存在（共 {len(versions)} 个历史版本）")

    target_srr = target.get("srr", {})
    if not isinstance(target_srr, dict) or not target_srr:
        raise HTTPException(status_code=400, detail="目标版本无有效 SRR 数据")

    # 当前 SRR 先保存为历史版本
    SNAPSHOT_KEYS = {"_clarify_state", "_kb_query_logs", "_clarify_history", "_version_history"}
    current_snapshot = {k: v for k, v in old_srr.items() if k not in SNAPSHOT_KEYS}
    current_title = (old_srr.get("requirement", {}) or {}).get("title", record.title)
    versions.append({
        "version_index": len(versions) + 1,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "title": current_title,
        "trigger": "restore",
        "extra_notes": f"切换至 v{version_index} 前自动快照",
        "srr": current_snapshot,
    })

    # 用目标版本 SRR 替换当前
    restored_srr = dict(target_srr)
    restored_srr["_version_history"] = versions
    # 保留 KB 查询日志 + 澄清历史
    for preserve_key in ("_kb_query_logs", "_clarify_history"):
        if preserve_key in old_srr:
            restored_srr[preserve_key] = old_srr[preserve_key]
    record.srr_json = restored_srr
    record.status = "pending_review"
    await session.commit()

    logger.info(
        "[API/requirements] 版本切换: req_id={}, v{} -> restored, new_history_count={}",
        req_id, version_index, len(versions),
    )
    return ApiResponse(
        success=True,
        message=f"已切换至 v{version_index}",
        data={"req_id": req_id, "version_index": version_index, "status": "pending_review"},
    )


@router.post("/{req_id}/regenerate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def regenerate_requirement(
    req_id: str,
    extra_notes: str = Form(default="", description="用户补充说明（可选，与文件至少一项）"),
    files: list[UploadFile] = File(default=[], description="重新上传的需求文件（.md / .docx / .txt，可选）"),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """补充信息后异步重新生成需求分析（SRR）。

    v2.15 异步化（变更组 C）：
        1. 同步：构建增强文本
        2. 创建 AnalysisTask（task_type=regenerate）
        3. 启动后台协程 _run_regenerate_reparse
        4. 立即返回 task_id，前端轮询进度

    v2.17 支持补充文件上传：
        - 用户可上传补充需求文件（.md / .docx / .txt），补充文件不替代原始文件
        - 补充文件可纠正原始文件中的错误，AI 分析时以原始 + 补充合并内容为准
        - 原始文件内容保留在 raw_text 字段，补充文件内容存入 supplementary_text 字段
        - 也可仅通过 extra_notes 提供补充说明（保持原有行为）
        - 文件和补充说明至少提供一项

    允许在 status=active（已澄清/已确认）后调用。
    操作记录追加到 _clarify_history。

    Args:
        req_id: 需求 ID
        extra_notes: 用户补充说明（可选）
        files: 补充需求文件列表（可选，不替代原始文件）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 task_id / req_id / status=analyzing / previous_title
    """
    extra_notes = extra_notes.strip()
    has_files = bool(files) and any(f.filename for f in files)

    # 文件和补充说明至少提供一项
    if not extra_notes and not has_files:
        raise HTTPException(status_code=400, detail="请至少提供补充说明或上传补充文件中的一项")

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 解析补充文件（如有），存入 supplementary_text，不替换原始 raw_text
    supplementary_text: str | None = None
    if has_files:
        valid_files = [f for f in files if f.filename]
        supplementary_text, _supp_images = await _parse_uploaded_files(valid_files, session=session)
        logger.info(
            "[API/requirements] 重新生成时上传了 {} 个补充文件，supplementary_text 长度={}",
            len(valid_files), len(supplementary_text),
        )

    old_srr = dict(record.srr_json or {})
    previous_title = (old_srr.get("requirement", {}) or {}).get("title", record.title)

    # 构建增强文本：原始文本 + 补充文件内容 + 已有 KB 补全 + 用户补充说明
    # 补充文件不替代原始文件，而是追加（补充文件可纠正原始文件中的错误）
    enhanced_parts = [record.raw_text or ""]

    if supplementary_text:
        enhanced_parts.append(f"\n\n## 补充需求文件内容（重新生成）\n{supplementary_text}")

    kb_augments = (old_srr.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        enhanced_parts.append("\n\n## KB 自动补全信息\n")
        for aug in kb_augments:
            enhanced_parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")

    if extra_notes:
        enhanced_parts.append(f"\n\n## 补充说明（重新生成）\n{extra_notes}")
    enhanced_raw = "\n".join(enhanced_parts)

    logger.info(
        "[API/requirements] 启动异步重新生成 SRR: req_id={}, has_files={}, extra_notes_len={}, enhanced_len={}",
        req_id, has_files, len(extra_notes), len(enhanced_raw),
    )

    # v2.16: 冲突检测
    await _check_no_running_task(req_id)

    # v2.15: 标记需求为 analyzing 状态
    record.status = "analyzing"
    await session.commit()

    # 创建 AnalysisTask + 启动后台协程
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="regenerate",
        req_id=req_id,
        product_id=record.product_id,
        status="pending",
        phase="parse",
        progress=0,
        message="已接收补充信息，准备重新生成..." if not has_files else "已接收补充文件，准备重新生成...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    asyncio.create_task(_run_regenerate_reparse(
        task_id=task_id,
        req_id=req_id,
        enhanced_raw=enhanced_raw,
        product_id=record.product_id,
        previous_title=previous_title,
        old_srr=old_srr,
        extra_notes=extra_notes,
        supplementary_text=supplementary_text,
    ))

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="regenerate_requirement", target_type="requirement", target_id=req_id,
        detail={"has_files": has_files, "extra_notes_len": len(extra_notes)},
    )

    return ApiResponse(
        success=True,
        message="已接收补充信息，正在后台重新生成",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "previous_title": previous_title,
        },
    )


async def _run_regenerate_reparse(
    task_id: str,
    req_id: str,
    enhanced_raw: str,
    product_id: str,
    previous_title: str,
    old_srr: dict,
    extra_notes: str,
    supplementary_text: str | None = None,
) -> None:
    """后台执行重新生成 SRR（异步协程，独立 session）。

    v2.20 变更：重新生成不再从原始文件解析，而是基于现有 SRR + 补充说明修改。
    流程：
        1. phase=parse: req_agent.refine_srr() 基于现有 SRR 修改生成新 SRR
           （refine_srr 内部已完成 FP 提取，结果存于 req_agent.last_fps）
        2. 保留 KB 日志 + 澄清历史 + KB 基线补全
        3. 持久化 + 更新标题（若有补充文件则同步更新 supplementary_text）
        4. 用 refine 已提取的 FP 替换旧 FP
        5. status=completed

    v2.0 历史版本管理：
        重新生成前把原 srr_json 完整快照保存到 _version_history 数组，
        保留历次重新分析前的需求分析结果，供用户回溯。

    v2.17 支持补充文件上传：
        若 supplementary_text 非空，持久化时更新 supplementary_text 字段。
        原始 raw_text 保持不变，补充文件内容独立存储，前端可分别查看。
        v2.20: 补充文件内容会合并进 refine 的 extra_notes 传给 LLM。
    """
    from sqlalchemy import update as sa_update, delete as sa_delete
    try:
        await _update_task(task_id, status="running", phase="parse", progress=30,
                           message="正在基于现有 SRR 调用 LLM 修改生成...")

        # v2.20: 合并 extra_notes 和 supplementary_text 作为 refine 的补充说明
        refine_notes_parts: list[str] = []
        if supplementary_text:
            refine_notes_parts.append(f"【补充需求文件内容】\n{supplementary_text}")
        if extra_notes:
            refine_notes_parts.append(f"【补充修改说明】\n{extra_notes}")
        refine_notes = "\n\n".join(refine_notes_parts) if refine_notes_parts else extra_notes

        # v2.20: 基于现有 SRR 修改，而非从原始文件重新解析
        final_srr = await req_agent.refine_srr(
            current_srr=old_srr,
            extra_notes=refine_notes,
            product_id=product_id,
        )
        logger.info("[Task/Regenerate] 基于 SRR 修改生成成功: req_id={}", req_id)

        # v2.32: 捕获 refine_srr 内部 _fp_extract_from_raw 产出的 interaction_srr
        # 用于后续合并到总 SRR 的 cross_fp_interactions（与需求分析流程一致）
        captured_interaction_srr = (
            dict(req_agent.interaction_srr) if req_agent.interaction_srr else None
        )

        await _update_task(task_id, phase="parse", progress=50, message="SRR 修改完成，正在检索知识库...")

        # ===== v2.32: phase=kb_search + clarify（与需求分析流程一致）=====
        # 重新生成同样需要走澄清环节：KB 检索 + 问题生成 + 质量评分
        clarify_result = None
        questions: list = []
        clarify_error: str | None = None
        async with async_session() as s:
            try:
                clarify_result = await clarify_agent.clarify(final_srr, session=s)
                questions = clarify_result.get("questions", [])
            except Exception as e:
                # v2.18: 关键错误不再静默——中断流水线，保存已生成结果，标记任务失败
                clarify_error = str(e)[:500]
                logger.error("[Task/Regenerate] ClarifyAgent 检测失败（关键错误，中断流水线）: {}", clarify_error)

        # v2.18: ClarifyAgent 失败时中断流水线，但保存已生成的 SRR+FP 供用户查看
        if clarify_error:
            base_srr = dict(final_srr)
            if captured_interaction_srr:
                base_srr = _merge_interaction_srr(base_srr, captured_interaction_srr)
            base_srr["_clarify_error"] = clarify_error
            async with async_session() as save_s:
                await save_s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(
                    status="draft",
                    srr_json=base_srr,
                ))
                try:
                    new_fps = req_agent.last_fps or []
                    if new_fps:
                        from app.db.models import FunctionalPoint as FP, FPDependency
                        old_fp_rows = (await save_s.execute(
                            select(FP.id).where(FP.requirement_id == req_id)
                        )).scalars().all()
                        if old_fp_rows:
                            old_ids_set = set(old_fp_rows)
                            await save_s.execute(sa_delete(FPDependency).where(
                                (FPDependency.source_fp_id.in_(old_ids_set)) |
                                (FPDependency.target_fp_id.in_(old_ids_set))
                            ))
                            await save_s.execute(sa_delete(FP).where(FP.requirement_id == req_id))
                            await save_s.flush()
                        await _save_fps_to_db(save_s, req_id, new_fps, "P2")
                        await _save_fp_dependencies(save_s, req_id, req_agent.last_fp_dependencies or [])
                        await _update_fp_index_in_srr(save_s, req_id)
                except Exception as fp_e:
                    logger.error("[Task/Regenerate] ClarifyAgent 失败后基础 FP 保存也失败: {}", str(fp_e)[:200])
                await save_s.commit()

            err_msg = f"知识库检索/需求澄清失败: {clarify_error}"
            await _update_task(
                task_id, status="failed", phase="kb_search", error=err_msg,
                message="重新生成失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
            )
            logger.error("[Task/Regenerate] 重新生成因 ClarifyAgent 失败而中断: task_id={}, req_id={}", task_id, req_id)
            return

        # v2.24: 记录 ClarifyAgent 子阶段耗时
        if clarify_result and clarify_result.get("phase_breakdown"):
            try:
                async with async_session() as s:
                    task = await s.get(AnalysisTask, task_id)
                    if task and task.phase_timings:
                        timings = dict(task.phase_timings)
                        kb_phase = dict(timings.get("kb_search", {}))
                        kb_phase["sub_breakdown"] = clarify_result["phase_breakdown"]
                        timings["kb_search"] = kb_phase
                        task.phase_timings = timings
                        await s.commit()
            except Exception as e:
                logger.warning("[Task/Regenerate] 写入 phase_breakdown 失败: {}", str(e)[:120])

        await _update_task(
            task_id, phase="clarify", progress=75,
            message=(f"知识库检索完成，检测到 {len(questions)} 个待补充问题" if questions
                     else "知识库检索完成，正在生成最终分析..."),
        )

        # 构建澄清后的 SRR：优先使用 KB 增强后的 augmented_srr
        multi_augmented = clarify_result.get("augmented_srr", final_srr) if clarify_result else final_srr
        final_srr = dict(multi_augmented)

        needs_clarify = (
            (clarify_result.get("status") == "needs_clarification") if clarify_result
            else (len(questions) > 0)
        )
        # v2.32: 重新生成完成后进入 draft（需澄清）或 pending_review（待确认）
        final_status = "draft" if needs_clarify else "pending_review"

        await _update_task(task_id, phase="clarify", progress=85, message="SRR 生成完成，正在保存...")

        # 记录历史
        new_title = (final_srr.get("requirement", {}) or {}).get("title", "")
        history_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "regenerate",
            "extra_notes": extra_notes,
            "previous_title": previous_title,
            "new_title": new_title,
        }
        clarify_history = list(old_srr.get("_clarify_history", []))
        clarify_history.append(history_entry)

        final_data = dict(final_srr)

        # v2.32: 写入澄清状态（与需求分析流程一致）
        if clarify_result:
            final_data["_clarify_state"] = {
                "questions": questions,
                "auto_augmentations": clarify_result.get("auto_augmentations", []),
                "kb_status": clarify_result.get("kb_status", {}),
                "quality_report": clarify_result.get("quality_report", {}),
                "field_conflicts": clarify_result.get("field_conflicts", []),
            }
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                final_data["_kb_query_logs"] = kb_logs
        # 保留历史 KB 查询日志（本次未产出新日志时回退使用历史日志）
        if "_kb_query_logs" not in final_data and old_srr.get("_kb_query_logs"):
            final_data["_kb_query_logs"] = old_srr.get("_kb_query_logs")

        # ===== v2.0 历史版本管理：把原 srr_json 完整快照保存到 _version_history =====
        # 快照只保留业务字段（剔除内部状态字段，避免重复堆积）
        SNAPSHOT_EXCLUDE_KEYS = {"_clarify_state", "_kb_query_logs", "_clarify_history", "_version_history"}
        old_snapshot = {k: v for k, v in old_srr.items() if k not in SNAPSHOT_EXCLUDE_KEYS}
        version_history = list(old_srr.get("_version_history", []))
        version_history.append({
            "version_index": len(version_history) + 1,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "title": previous_title or old_snapshot.get("requirement", {}).get("title", ""),
            "trigger": "regenerate",
            "extra_notes": extra_notes,
            "srr": old_snapshot,
        })
        final_data["_version_history"] = version_history
        # ===== end 历史版本管理 =====

        # 保留澄清历史
        final_data["_clarify_history"] = clarify_history
        # 迁移 KB 基线补全内容（v2.40: 移除 additional_details，仅保留 _baseline_context）
        old_baseline = old_srr.get("_baseline_context")
        if old_baseline:
            final_data["_baseline_context"] = old_baseline

        # v2.32: 合并 interaction_srr 到 cross_fp_interactions（与需求分析流程一致）
        if captured_interaction_srr:
            final_data = _merge_interaction_srr(final_data, captured_interaction_srr)

        # v2.32: 保留旧 SRR 的 legacy_data_impact（用户在之前澄清环节填写的影响范围）
        # 重新生成流程本身不处理用户澄清答案（答案在后续 _run_clarify_reparse 中处理），
        # 但需保留旧的 legacy_data_impact.user_clarification，避免重新生成后数据丢失。
        # 若 LLM 重新生成了 legacy_data_impact（is_optimization=True），则尊重 LLM 判断，
        # 仅在新 user_clarification 为空时回填旧值。
        try:
            new_ldi = final_data.get("legacy_data_impact")
            old_ldi = old_srr.get("legacy_data_impact")
            old_has = isinstance(old_ldi, dict) and old_ldi.get("is_optimization") and old_ldi.get("user_clarification", "").strip()
            if old_has:
                if isinstance(new_ldi, dict) and new_ldi.get("is_optimization"):
                    # LLM 重新生成了优化需求分析，回填旧的用户澄清（若新值为空）
                    if not str(new_ldi.get("user_clarification", "")).strip():
                        new_ldi["user_clarification"] = old_ldi.get("user_clarification", "")
                        logger.info(
                            "[Task/Regenerate] 回填旧 user_clarification 到新 legacy_data_impact: req_id={}", req_id,
                        )
                else:
                    # LLM 未生成优化需求分析，保留旧的 legacy_data_impact
                    final_data["legacy_data_impact"] = old_ldi
                    logger.info(
                        "[Task/Regenerate] 保留旧 SRR 的 legacy_data_impact: req_id={}", req_id,
                    )
        except Exception as ldi_e:
            logger.warning("[Task/Regenerate] 保留 legacy_data_impact 失败(不阻断): {}", str(ldi_e)[:200])

        # 持久化
        async with async_session() as s:
            values: dict = {"srr_json": final_data, "status": final_status}
            if new_title:
                values["title"] = new_title
            # v2.17: 若用户上传了补充文件，更新 supplementary_text（原始 raw_text 保持不变）
            if supplementary_text:
                values["supplementary_text"] = supplementary_text
            # v2.33: 自增 revision（重新生成属于内容变更）
            values["revision"] = Requirement.revision + 1
            await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(**values))
            await s.commit()

        # v2.20: refine_srr 内部已调用 _fp_extract_from_raw 提取 FP，
        # 直接使用 req_agent.last_fps / last_fp_dependencies，不再重复从 raw_text 提取
        try:
            new_fps = req_agent.last_fps or []
            new_fp_deps = req_agent.last_fp_dependencies or []
            if new_fps:
                # 使用独立 session，不依赖 HTTP 请求的 session（此函数在后台协程运行）
                async with async_session() as fp_split_sess:
                    record2 = (await fp_split_sess.execute(
                        select(Requirement).where(Requirement.id == req_id)
                    )).scalar_one_or_none()
                    if record2:
                        logger.info("[Task/Regenerate] 应用 refine 已提取的 FP: req_id={}, fp_count={}",
                                    req_id, len(new_fps))
                        from app.db.models import FunctionalPoint as FP, FPDependency
                        old_fp_rows = (await fp_split_sess.execute(
                            select(FP.id).where(FP.requirement_id == req_id)
                        )).scalars().all()
                        if old_fp_rows:
                            old_ids_set = set(old_fp_rows)
                            await fp_split_sess.execute(
                                sa_delete(FPDependency).where(
                                    (FPDependency.source_fp_id.in_(old_ids_set)) |
                                    (FPDependency.target_fp_id.in_(old_ids_set))
                                )
                            )
                            await fp_split_sess.execute(sa_delete(FP).where(FP.requirement_id == req_id))
                            await fp_split_sess.flush()
                        await _save_fps_to_db(fp_split_sess, req_id, new_fps, record2.priority)
                        await _save_fp_dependencies(fp_split_sess, req_id, new_fp_deps)
                        # v2.32: interaction_srr 已在上方合并到 final_data，此处只需更新 fp_index
                        await _update_fp_index_in_srr(fp_split_sess, req_id)  # v2.18: 更新总 SRR 的 fp_index
                        await fp_split_sess.commit()
                        logger.info("[Task/Regenerate] FP 应用完成: req_id={}, new_fps={}", req_id, len(new_fps))
            else:
                logger.warning("[Task/Regenerate] refine 未产出 FP（last_fps 为空），跳过 FP 更新: req_id={}", req_id)
        except Exception as fp_err:
            # v2.18: FP 应用失败不再静默——中断流水线，标记任务失败
            fp_err_msg = str(fp_err)[:500]
            logger.error("[Task/Regenerate] FP 应用失败（关键错误，中断流水线）: req_id={}, err={}", req_id, fp_err_msg)
            err_msg = f"功能点(FP)应用失败: {fp_err_msg}"
            await _update_task(
                task_id, status="failed", phase="done", error=err_msg,
                message="重新生成失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
            )
            return

        status_desc = "待确认" if final_status == "pending_review" else "待澄清"
        logger.info(
            "[Task/Regenerate] 重新生成完成，需求进入{}: req_id={}, title={}->{}, history_versions={}",
            status_desc, req_id, previous_title, new_title, len(version_history),
        )

        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message=f"需求分析已重新生成，进入{status_desc}",
            result_json={"req_id": req_id, "new_title": new_title, "status": final_status,
                         "version_count": len(version_history),
                         "needs_clarification": needs_clarify,
                         "question_count": len(questions)},
            completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task/Regenerate] 重新生成完成: task_id={}, req_id={}", task_id, req_id)

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task/Regenerate] 重新生成失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        await _update_task(
            task_id, status="failed", error=err_msg,
            message="重新生成失败: " + err_msg[:200],
            completed_at=datetime.now(timezone.utc),
        )
        # 失败时回退状态
        try:
            async with async_session() as s:
                await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(status="active"))
                await s.commit()
        except Exception as revert_err:
            logger.warning("[Task/Regenerate] 失败回退状态失败: req_id={}, error={}", req_id, str(revert_err)[:150])


async def _run_fp_merge_task(
    task_id: str, req_id: str, fp_ids: list[str],
    merged_name: str, product_id: str,
) -> None:
    """后台协程：执行功能点 LLM 合并。

    Args:
        task_id: AnalysisTask ID
        req_id: 需求 ID
        fp_ids: 待合并的功能点 ID 列表
        merged_name: 用户指定的合并后名称
        product_id: 产品 ID
    """
    try:
        await _update_task(task_id, status="running", progress=10, message="正在加载功能点...")
        async with async_session() as s:
            fps_to_merge = []
            for fid in fp_ids:
                fp = await s.get(FunctionalPoint, fid)
                if fp and fp.requirement_id == req_id:
                    fps_to_merge.append(fp)
            if len(fps_to_merge) < 2:
                raise ValueError("有效功能点不足，无法合并")

            # 序列化功能点子 SRR
            import json as _j
            fp_jsons = []
            for fp in fps_to_merge:
                d = {"name": fp.name, "fp_type": fp.fp_type, "test_hints": fp.test_hints or []}
                if isinstance(fp.srr_json, dict):
                    d["sub_srr"] = fp.srr_json
                fp_jsons.append(d)

        custom_name_hint = f"\n\n**用户指定的合并后名称：{merged_name}**" if merged_name else ""
        merge_prompt = f"""将以下多个功能点合并为一个完整的功能点。

这些功能点原本属于同一个业务功能，因需求拆解过细被分成了多个。请按标准 SRR 格式重新生成一份**完整**的子 SRR，保证所有功能细节不遗漏。{custom_name_hint}

合并规则：
- 如果上面提供了用户指定名称，使用该名称；否则取一个最能概括所有子功能点的名称
- fp_type 选最匹配的（workflow/constraint/calculation/api_behavior/data_record/config）
- flows.happy_path.steps：将各子 FP 的步骤整合为一条完整的步骤链
- flows.alternate_flows：所有异常分支合并且去重
- boundary_conditions / business_rules / data_constraints / acceptance_criteria / preconditions：合并去重
- test_hints：合并去重

原始功能点：
{_j.dumps(fp_jsons, ensure_ascii=False, indent=2)}

请输出合并后的功能点 JSON。子 SRR 必须使用标准格式。"""

        await _update_task(task_id, progress=30, message="LLM 正在合并功能点...")
        merged = await llm_client.call_json(
            prompt=merge_prompt,
            system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
            schema_hint={"name": str, "fp_type": str, "summary": str, "suggested_module": str, "sub_srr": dict, "test_hints": list},
            temperature=0.2,
            agent_name="FpAgent",
        )

        # 持久化
        await _update_task(task_id, progress=80, message="保存合并结果...")
        async with async_session() as s:
            primary_fp = await s.get(FunctionalPoint, fp_ids[0])
            merged_id = gen_requirement_id()

            # v2.33: 预检查重名——若合并后名称与同需求下其他 FP 重名，自动加后缀
            # 收集旧 FP 名称映射（用于后续 cross_fp_interactions 增量更新）
            old_names = {fp.name for fp in fps_to_merge}
            final_merged_name = merged_name or merged.get("name", "合并功能点")
            # 查询同需求下不在合并列表中的 FP 名称
            from sqlalchemy import select as _sa_sel
            existing_names_result = await s.execute(
                _sa_sel(FunctionalPoint.name).where(
                    FunctionalPoint.requirement_id == req_id,
                    ~FunctionalPoint.id.in_(set(fp_ids)),
                )
            )
            existing_names = {row[0] for row in existing_names_result.all()}
            if final_merged_name in existing_names:
                suffix = "（合并）"
                candidate = f"{final_merged_name}{suffix}"
                idx = 2
                while candidate in existing_names:
                    candidate = f"{final_merged_name}{suffix}{idx}"
                    idx += 1
                logger.warning(
                    "[Task/FP-Merge] 合并后名称「{}」与已有 FP 重名，自动调整为「{}」: req_id={}",
                    final_merged_name, candidate, req_id,
                )
                final_merged_name = candidate

            merged_fp = FunctionalPoint(
                id=merged_id, requirement_id=req_id,
                module_id=primary_fp.module_id if primary_fp else None,
                name=final_merged_name,
                fp_type=merged.get("fp_type", "workflow"),
                priority="P1", status="pending_review",
                srr_json=merged.get("sub_srr", {}),
                test_hints=merged.get("test_hints", []),
                atomicity_check={"trigger": True, "result": True, "boundary": True},
            )
            s.add(merged_fp)

            # 删除旧 FP
            from sqlalchemy import delete as _del
            old_ids = set(fp_ids)
            await s.execute(_del(FPDependency).where(
                (FPDependency.source_fp_id.in_(old_ids)) | (FPDependency.target_fp_id.in_(old_ids))
            ))
            for fid in fp_ids:
                fp = await s.get(FunctionalPoint, fid)
                if fp:
                    await s.delete(fp)
            await s.flush()

            # v2.18 P3: 代码层去重检测——合并后 FP 的 business_rules 不应与总 SRR 的 shared_business_rules 重复
            try:
                req_result = await s.execute(
                    select(Requirement.srr_json).where(Requirement.id == req_id)
                )
                req_row = req_result.first()
                if req_row and req_row[0]:
                    total_srr = req_row[0] if isinstance(req_row[0], dict) else {}
                    shared_rules = total_srr.get("shared_business_rules", [])
                    merged_rules = (merged_fp.srr_json or {}).get("business_rules", [])
                    if shared_rules and merged_rules:
                        shared_texts = {str(r.get("rule", r) if isinstance(r, dict) else r).strip() for r in shared_rules}
                        duplicates = [
                            r for r in merged_rules
                            if str(r.get("rule", r) if isinstance(r, dict) else r).strip() in shared_texts
                        ]
                        if duplicates:
                            logger.warning(
                                "[FP-Merge] 检测到 {} 条重复规则（合并后 FP 与总 SRR shared_business_rules 重复）: req_id={}, fp_id={}",
                                len(duplicates), req_id, merged_fp.id,
                            )
            except Exception as dedup_e:
                logger.warning("[FP-Merge] 去重检测失败（不阻断）: {}", str(dedup_e)[:150])

            # v2.18: 更新总 SRR 的 fp_index（FP 列表已变化）
            await _update_fp_index_in_srr(s, req_id)
            # v2.33: 增量更新 cross_fp_interactions 中的旧 FP 名称引用
            await _update_cross_fp_interactions_on_merge(s, req_id, old_names, merged_fp.name)
            await s.commit()

        logger.info("[Task/FP-Merge] 合并完成: {} → {}, req={}", fp_ids, merged_fp.name, req_id)

        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message=f"已将 {len(fp_ids)} 个功能点合并为「{merged_fp.name}」",
            result_json={"fp_id": merged_fp.id, "name": merged_fp.name, "req_id": req_id},
            completed_at=datetime.now(timezone.utc),
        )

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task/FP-Merge] 合并失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        await _update_task(
            task_id, status="failed", error=err_msg,
            message="功能点合并失败: " + err_msg[:200],
            completed_at=datetime.now(timezone.utc),
        )


# ============================================================================
# 6. 编辑需求（直接更新 SRR JSON）
# ============================================================================

@router.put("/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_requirement(
    req_id: str,
    req: RequirementUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """更新需求的 SRR JSON（手动编辑）。

    用户可以直接修改 SRR 中的任意字段，修改后的 SRR 会立即持久化。
    适用于：
    - 修复 LLM 解析错误
    - 用户主动补充/修改字段内容
    - 应用澄清答案后微调

    Args:
        req_id: 需求 ID
        req: 更新请求，含 srr_json（必须）和 raw_text（可选）
        session: 数据库会话
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 只更新需要的字段
    record.srr_json = req.srr_json
    if req.raw_text is not None:
        record.raw_text = req.raw_text

    # 同步顶层元数据（从 SRR 同步）
    srr = req.srr_json
    req_data = srr.get("requirement", {})
    record.title = req_data.get("title", record.title)
    record.priority = req_data.get("priority", record.priority)
    record.modules = req_data.get("modules", record.modules)
    # v2.33: 自增 revision（手动编辑属于内容变更）
    record.revision = (record.revision or 0) + 1

    await session.commit()

    logger.info("[API/requirements] 需求已更新: req_id={}, revision={}", req_id, record.revision)

    return ApiResponse(
        success=True,
        message="需求已更新",
        data={"req_id": req_id},
    )


# ============================================================================
# 7. 删除需求（已归档不可删除）
# ============================================================================

@router.get("/{req_id}/delete-preview", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def delete_preview(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """预览删除需求将产生的级联影响。

    返回各类依赖记录的计数，供前端弹窗提示用户确认。
    """
    record = await session.get(Requirement, req_id)
    if not record:
        raise HTTPException(status_code=404, detail="需求不存在")

    from sqlalchemy import func as sql_func

    # 子表计数
    tc_count = await session.scalar(
        select(sql_func.count()).select_from(TestCase).where(TestCase.req_id == req_id)
    )
    gb_count = await session.scalar(
        select(sql_func.count()).select_from(GenerationBatch).where(GenerationBatch.req_id == req_id)
    )
    dep_count = await session.scalar(
        select(sql_func.count()).select_from(ReqDependency).where(
            (ReqDependency.source_req_id == req_id) | (ReqDependency.target_req_id == req_id)
        )
    )
    at_count = await session.scalar(
        select(sql_func.count()).select_from(AnalysisTask).where(AnalysisTask.req_id == req_id)
    )
    tr_count = await session.scalar(
        select(sql_func.count()).select_from(Traceability).where(Traceability.requirement_id == req_id)
    )
    fp_count = await session.scalar(
        select(sql_func.count()).select_from(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
    )
    # superseded_by / supersedes
    super_count = await session.scalar(
        select(sql_func.count()).select_from(Requirement).where(
            (Requirement.superseded_by == req_id) | (Requirement.supersedes == req_id)
        )
    )

    return ApiResponse(success=True, message="ok", data={
        "req_id": req_id,
        "title": record.title,
        "status": record.status,
        "dependencies": {
            "test_cases": tc_count or 0,
            "generation_batches": gb_count or 0,
            "req_dependencies": dep_count or 0,
            "analysis_tasks": at_count or 0,
            "traceability": tr_count or 0,
            "functional_points": fp_count or 0,
            "superseded_refs": super_count or 0,
        },
    })


@router.delete("/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def delete_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除需求。

    规则（v2.1 设计；v2.20 权限放宽为 require_write）：
    - active / superseded / deprecated 状态 → 允许删除（从 DB 永久移除）
    - archived 状态 → ❌ 拒绝删除（已归档进入知识库，删除会导致 KB 数据不一致）

    如果需要删除已归档需求，请先将其状态改为 active 后再删除。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status == "archived":
        raise HTTPException(
            status_code=409,
            detail=(
                f"需求 {req_id} 已归档，无法删除。"
                "已归档的需求已进入知识库，删除会导致知识库数据不一致。"
                "如需删除，请先将状态改为「活动中」后再操作。"
            ),
        )

    logger.info("[API/requirements] 删除需求: req_id={}, status={}", req_id, record.status)

    # ---- 级联清理子表（无 ondelete=CASCADE 的 FK，显式删除）----
    from sqlalchemy import delete as sql_delete, update as sql_update

    # 测试用例
    tc_count = await session.execute(sql_delete(TestCase).where(TestCase.req_id == req_id))
    # 生成批次
    gb_count = await session.execute(sql_delete(GenerationBatch).where(GenerationBatch.req_id == req_id))
    # 需求间依赖
    dep_count = await session.execute(
        sql_delete(ReqDependency).where(
            (ReqDependency.source_req_id == req_id) | (ReqDependency.target_req_id == req_id)
        )
    )
    # 异步分析任务（req_id 可为 NULL，直接删除）
    at_count = await session.execute(sql_delete(AnalysisTask).where(AnalysisTask.req_id == req_id))
    # 需求-用例追溯
    tr_count = await session.execute(sql_delete(Traceability).where(Traceability.requirement_id == req_id))

    # 功能点（虽有 ondelete=CASCADE，但 FPDependency 也需清理）
    fp_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_result.fetchall()}
    if fp_ids:
        await session.execute(
            sql_delete(FPDependency).where(
                (FPDependency.source_fp_id.in_(fp_ids)) | (FPDependency.target_fp_id.in_(fp_ids))
            )
        )
        await session.execute(
            sql_delete(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
        )

    # 清理其他需求对当前需求的引用（superseded_by / supersedes）
    await session.execute(
        sql_update(Requirement).where(Requirement.superseded_by == req_id).values(superseded_by=None)
    )
    await session.execute(
        sql_update(Requirement).where(Requirement.supersedes == req_id).values(supersedes=None)
    )

    logger.info(
        "[API/requirements] 清理子表: "
        "test_cases=%d, gen_batches=%d, req_deps=%d, analysis_tasks=%d, traceability=%d, fp=%d",
        tc_count.rowcount, gb_count.rowcount, dep_count.rowcount, at_count.rowcount, tr_count.rowcount, len(fp_ids),
    )

    await session.delete(record)
    await session.commit()

    # 同步删除向量库中的记录（主需求向量 + FP-{fp_id} 向量，避免孤儿）
    try:
        from app.db.milvus_store import vector_store

        # 删除主需求向量
        await asyncio.to_thread(vector_store.delete_requirement_from_kb, req_id)
        logger.info("[API/requirements] 已同步删除向量库主记录: req_id={}", req_id)

        # P0 修复：级联删除 FP-{fp_id} 向量，避免孤儿
        for fp_id in fp_ids:
            try:
                await asyncio.to_thread(vector_store.delete_requirement_from_kb, f"FP-{fp_id}")
            except Exception as fp_err:
                logger.warning("[API/requirements] FP 向量删除失败(不阻断): fp_id={}, error={}", fp_id, str(fp_err)[:100])
        if fp_ids:
            logger.info("[API/requirements] 已同步删除 {} 个 FP 向量", len(fp_ids))
    except Exception as e:
        logger.warning("[API/requirements] 向量库删除失败(不阻断): req_id={}, error={}", req_id, str(e)[:100])

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="delete_requirement", target_type="requirement", target_id=req_id,
        detail={"status": record.status},
    )

    return ApiResponse(
        success=True,
        message=f"需求 {req_id} 已删除",
        data={"deleted": True, "status": record.status},
    )


# ============================================================================
# 8. 归档需求（active → archived，数据导入知识库）
# ============================================================================

class ArchiveRequest(BaseModel):
    """归档请求体。"""
    go_live_date: str | None = None

@router.post("/{req_id}/archive", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def archive_requirement(
    req_id: str,
    body: ArchiveRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """归档需求。

    规则：
    - active 状态 → 允许归档
    - draft 状态 → 拒绝归档（需先完成澄清）
    - archived / deprecated 状态 → 拒绝归档（已归档或已废弃）
    - 归档后数据写入向量知识库，供后续需求检索参考
    - 上线时间（go_live_date）用于 KB 检索时区分最新需求

    Args:
        req_id: 需求 ID
        body: 归档请求体，含 go_live_date（YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / status
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status == "draft":
        raise HTTPException(
            status_code=409,
            detail="需求尚未完成澄清（draft 状态），请先完成澄清后再归档",
        )

    if record.status == "archived":
        raise HTTPException(
            status_code=409,
            detail=f"需求 {req_id} 已归档，无需重复归档",
        )

    if record.status == "deprecated":
        raise HTTPException(
            status_code=409,
            detail="已废弃的需求无法归档",
        )

    # 解析上线时间（支持 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
    parsed_go_live = None
    if body and body.go_live_date:
        try:
            # 尝试解析为 datetime（含时分）
            parsed_go_live = datetime.fromisoformat(body.go_live_date)
        except ValueError:
            try:
                # 降级为 date（补零时时间）
                parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
                )

    # 查询项目版本号（用于 KB 检索版本溯源）
    project_version = ""
    if record.project_id:
        proj_result = await session.execute(
            select(Project.version).where(Project.id == record.project_id)
        )
        proj_row = proj_result.scalar_one_or_none()
        if proj_row:
            project_version = proj_row

    # 更新状态 + 上线日期 + 归档时间（真实持久化，区别于 updated_at）
    old_status = record.status
    record.status = "archived"
    record.go_live_date = parsed_go_live
    record.archived_at = datetime.now(timezone.utc)
    await session.commit()

    # 写入向量知识库（供后续需求检索参考）
    # 注意：向量化是异步任务（Celery）。此处仅“派发”任务，不代表向量已落库。
    # 因此字段命名为 kb_task_queued（而非 kb_written），避免误导“已写入”。
    kb_task_queued = False
    kb_error = ""
    milvus_meta = {
        # v2.34: 显式标记 type=requirement，便于检索时与 functional_point 隔离
        "type": "requirement",
        "product_id": record.product_id,
        "modules": ",".join(record.modules or []),
        "priority": record.priority,
        "status": "archived",
        "go_live_date": parsed_go_live.isoformat() if parsed_go_live else "",
        "project_version": project_version,
    }
    vectorization_status = "pending"
    try:
        from app.tasks.celery_app import celery_app

        if celery_app is not None:
            # 异步双写：PG 已 commit，Celery 异步计算 Embedding + 写入 Milvus
            celery_app.send_task(
                "app.tasks.embedding_tasks.embed_and_store_requirement",
                args=[record.id, record.srr_json or {}, milvus_meta],
                queue="embedding",
            )
            kb_task_queued = True
            vectorization_status = "processing"
            logger.info("[API/requirements] 异步写入任务已派发: req_id={}, go_live_date={}, project_version={}",
                        req_id, milvus_meta["go_live_date"], project_version)
        else:
            # Celery 未安装/未配置 → 降级为同步写入（同步即已落库）
            from app.db.milvus_store import vector_store
            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=record.id,
                srr_json=record.srr_json or {},
                metadata=milvus_meta,
            )
            kb_task_queued = True
            vectorization_status = "completed"
            logger.info("[API/requirements] 已写入知识库(同步降级): req_id={}, go_live_date={}, project_version={}",
                        req_id, milvus_meta["go_live_date"], project_version)
    except Exception as e:
        kb_error = str(e)[:300]
        logger.warning("[API/requirements] Celery 派发失败，尝试同步降级写入: req_id={}, error={}", req_id, kb_error[:200])
        # P4 修复：Celery 派发失败时立即同步降级写入，避免 KB 无数据
        try:
            from app.db.milvus_store import vector_store
            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=record.id,
                srr_json=record.srr_json or {},
                metadata=milvus_meta,
            )
            kb_task_queued = True
            vectorization_status = "completed"
            logger.info("[API/requirements] Celery 派发失败，同步写入成功: req_id={}", req_id)
        except Exception as sync_err:
            sync_error = str(sync_err)[:300]
            logger.error("[API/requirements] 同步降级写入也失败: req_id={}, error={}", req_id, sync_error[:200])
            vectorization_status = "failed"
            kb_error = sync_error

    # P4 修复：回写向量化状态到 Requirement，便于前端展示与运维监控
    if vectorization_status != "pending":
        try:
            from sqlalchemy import update as sa_update_vs
            async with async_session() as vs_session:
                await vs_session.execute(
                    sa_update_vs(Requirement).where(Requirement.id == req_id).values(
                        vectorization_status=vectorization_status,
                        vectorization_error=kb_error if vectorization_status == "failed" else None,
                    )
                )
                await vs_session.commit()
        except Exception as vs_err:
            logger.warning("[API/requirements] 向量化状态回写失败(不阻断): {}", str(vs_err)[:200])

    logger.info("[API/requirements] 归档成功: req_id={}, old_status={}, kb_task_queued={}", req_id, old_status, kb_task_queued)

    # v1.5.3: 归档时将 FP 及其依赖关系写入向量知识库
    # v2.3: 同步更新 FunctionalPoint.status = "archived"，使归档 FP 可被复用
    fp_kb_count = 0
    fp_archived_count = 0
    try:
        async with async_session() as fp_session:
            fp_result = await fp_session.execute(
                select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
            )
            all_fps = fp_result.scalars().all()
            for f in all_fps:
                # v2.3: 更新 FP 状态为 archived，标记 FP 进入可复用阶段
                if f.status != "archived":
                    f.status = "archived"
                    fp_archived_count += 1
                fp_meta = {
                    "type": "functional_point",
                    "requirement_id": req_id,
                    # v2.34: 补全 FP 元数据，支持按模块/功能点过滤检索
                    "fp_id": f.id or "",
                    "fp_name": f.name or "",
                    # v2.2: 多模块关联（替代 legacy 单 module_id）
                    "modules": ",".join(f.modules or []) if f.modules else (f.module_id or ""),
                    "module_id": f.module_id or "",  # 保留 legacy 字段向后兼容
                    "fp_type": f.fp_type,
                    "product_id": record.product_id,
                    "status": "archived",
                }
                # 构造 _srr_to_text 兼容的嵌入格式（复用子 SRR 的结构化字段）
                embed_srr = dict(f.srr_json) if isinstance(f.srr_json, dict) else {}
                embed_srr.setdefault("requirement", {})
                embed_srr["requirement"]["title"] = f.name
                # v2.34: 注入 modules 到 srr_json.requirement，便于 KB 检索时反查
                if f.modules:
                    embed_srr["requirement"]["modules"] = list(f.modules)
                elif f.module_id:
                    embed_srr["requirement"]["modules"] = [f.module_id]
                if not embed_srr["requirement"].get("summary"):
                    embed_srr["requirement"]["summary"] = (
                        f.srr_json.get("requirement", {}).get("summary", "")
                        if isinstance(f.srr_json, dict) else ""
                    ) or f.name
                # 同步写入 Milvus
                try:
                    from app.db.milvus_store import vector_store
                    await asyncio.to_thread(
                        vector_store.add_requirement,
                        req_id=f"FP-{f.id}",
                        srr_json=embed_srr,
                        metadata=fp_meta,
                    )
                    fp_kb_count += 1
                except Exception as e:
                    logger.warning("[Archive] FP 写入 Milvus 失败 fp_id={}: {}", f.id, str(e)[:150])
            if fp_archived_count > 0:
                await fp_session.commit()
                logger.info("[Archive] {} 个 FP 状态已更新为 archived (req={})", fp_archived_count, req_id)
    except Exception as e:
        logger.warning("[Archive] FP KB 写入失败（不阻断）: {}", str(e)[:200])

    message = "需求已归档，知识库写入任务已异步派发" if kb_task_queued else "需求已归档，但知识库写入任务派发失败（请检查 Celery 是否运行 / 嵌入模型是否就绪）"
    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="archive_requirement", target_type="requirement", target_id=req_id,
        detail={"old_status": old_status, "kb_task_queued": kb_task_queued},
    )
    return ApiResponse(
        success=True,
        message=message,
        data={
            "req_id": req_id,
            "status": "archived",
            "kb_task_queued": kb_task_queued,
            "kb_error": kb_error if not kb_task_queued else "",
            "go_live_date": parsed_go_live.isoformat() if parsed_go_live else "",
            "project_version": project_version,
            "fp_archived_count": fp_archived_count,
        },
    )


# ============================================================================
# 8.5 修改上线时间（已归档需求）
# ============================================================================

class UpdateGoLiveDateRequest(BaseModel):
    """修改上线时间请求体。"""
    go_live_date: str


@router.patch("/{req_id}/go-live-date", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_go_live_date(
    req_id: str,
    body: UpdateGoLiveDateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """修改已归档需求的上线时间。

    上线时间随时可能变更，此接口允许修改已归档需求的 go_live_date，
    并同步更新 Milvus 向量库中的元数据（用于 KB 检索时按上线时间排序）。

    Args:
        req_id: 需求 ID
        body: 含 go_live_date（YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / go_live_date / milvus_updated
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"仅已归档需求可修改上线时间（当前状态: {record.status}）",
        )

    # 解析上线时间
    try:
        parsed_go_live = datetime.fromisoformat(body.go_live_date)
    except ValueError:
        try:
            parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
            )

    old_go_live = record.go_live_date
    record.go_live_date = parsed_go_live
    await session.commit()

    logger.info(
        "[API/requirements] 上线时间已修改: req_id={}, {} → {}",
        req_id,
        old_go_live.isoformat() if old_go_live else "(空)",
        parsed_go_live.isoformat(),
    )

    # 同步更新 Milvus 向量库中的元数据（主需求 + FP 向量）
    milvus_updated = False
    milvus_error = ""
    try:
        from app.db.milvus_store import vector_store

        # 更新主需求向量
        await asyncio.to_thread(
            vector_store.update_requirement_metadata,
            req_id,
            {"go_live_date": parsed_go_live.isoformat()},
        )

        # P2 修复：同步更新该需求下所有 FP 向量的 go_live_date
        fp_result = await session.execute(
            select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
        )
        fp_ids = [r[0] for r in fp_result.fetchall()]
        fp_update_count = 0
        for fp_id in fp_ids:
            try:
                await asyncio.to_thread(
                    vector_store.update_requirement_metadata,
                    f"FP-{fp_id}",
                    {"go_live_date": parsed_go_live.isoformat()},
                )
                fp_update_count += 1
            except Exception as fp_err:
                logger.warning("[API/requirements] FP 向量元数据更新失败(不阻断): fp_id={}, error={}", fp_id, str(fp_err)[:100])
        if fp_ids:
            logger.info("[API/requirements] 已同步 {} 个 FP 向量的 go_live_date", fp_update_count)

        milvus_updated = True
    except Exception as e:
        milvus_error = str(e)[:300]
        logger.warning(
            "[API/requirements] Milvus 元数据更新失败（不阻断）: req_id={}, error={}",
            req_id, milvus_error[:200],
        )

    return ApiResponse(
        success=True,
        message="上线时间已更新" + ("，向量库元数据已同步" if milvus_updated else "，但向量库元数据同步失败"),
        data={
            "req_id": req_id,
            "go_live_date": parsed_go_live.isoformat(),
            "milvus_updated": milvus_updated,
            "milvus_error": milvus_error if not milvus_updated else "",
        },
    )


# ============================================================================
# 9. 影响分析（基于模块依赖图谱）
# ============================================================================

@router.get("/{req_id}/impact", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def analyze_requirement_impact(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """分析需求变更的影响范围（v2.35: 基于 FP 依赖图谱分析受影响功能点）。

    v2.35 变更：
        - 保留模块级影响分析（direct_modules / indirect_modules）作为参考
        - 移除 affected_requirements（不再分析受影响的需求）
        - 新增 affected_fps：基于 FP 依赖图谱 BFS 分析受影响的功能点
        - 已归档 FP 查询知识库展示功能点说明（kb_context）

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 direct_modules / indirect_modules / affected_fps / total_affected
    """
    # 1. 查询需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 2. 调用 ImpactAnalysisAgent 的完整影响分析（模块 BFS + FP 依赖图谱 BFS + KB 查询）
    from app.agents.impact_agent import impact_agent

    try:
        impact = await impact_agent.analyze_requirement_impact(req_id, session)
    except Exception as e:
        logger.error("[API/requirements] 影响分析失败: {}", str(e)[:200])
        raise HTTPException(status_code=500, detail=f"影响分析失败: {str(e)[:200]}")

    if "error" in impact:
        raise HTTPException(status_code=404, detail=impact["error"])

    # 3. 查询模块名称，过滤孤儿依赖（模块已删除但依赖记录还在）
    mod_impact = impact.get("module_impact", {})
    direct_mod_ids = mod_impact.get("direct", [])
    indirect_mods = mod_impact.get("indirect", [])
    all_mod_ids = [m if isinstance(m, str) else m.get("module_id", "") for m in direct_mod_ids]
    all_mod_ids += [m.get("module_id", "") for m in indirect_mods if m.get("module_id")]
    all_mod_ids = [m for m in all_mod_ids if m]

    mod_names: dict[str, str] = {}
    valid_ids: set[str] = set()
    if all_mod_ids:
        from app.db.models import Module as ModuleModel
        mod_stmt = select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_mod_ids))
        mod_result = await session.execute(mod_stmt)
        mod_names = {row[0]: row[1] for row in mod_result.all()}
        valid_ids = set(mod_names.keys())

    # 4. 构建返回数据
    req_info = impact.get("requirement", {})
    affected_fps = impact.get("affected_fps", [])

    return ApiResponse(
        success=True,
        message=f"影响分析完成：{len(direct_mod_ids)} 个直接模块，"
                f"{len(indirect_mods)} 个间接模块，"
                f"{len(affected_fps)} 个受影响功能点",
        data={
            "req_id": req_id,
            "req_title": req_info.get("title", record.title),
            "direct_modules": [
                {"id": m, "name": mod_names.get(m, f"已删除模块({m})")}
                for m in direct_mod_ids
                if m in valid_ids
            ],
            "indirect_modules": [
                {
                    "id": m.get("module_id", ""),
                    "name": mod_names.get(m.get("module_id", ""), f"已删除模块({m.get('module_id', '')})"),
                    "reason": m.get("reason", ""),
                    "depth": m.get("depth", 1),
                }
                for m in indirect_mods
                if m.get("module_id", "") in valid_ids
            ],
            "affected_fps": affected_fps,
            "total_affected": impact.get("total_affected", len(affected_fps)),
        },
    )


# ============================================================================
# v1.4 功能点（FunctionalPoint）API
# ============================================================================

from app.core import prompt_manager as pm
from app.core.llm_client import llm_client
from app.utils.id_gen import gen_requirement_id  # 复用 ID 生成


class FPDependencyCreateRequest(BaseModel):
    """FP 依赖创建请求。"""
    source_fp_id: str
    target_fp_id: str
    relation_type: str = "depends_on"
    description: str = ""


async def _load_active_requirement(req_id: str, session: AsyncSession) -> Requirement:
    """加载需求并校验非归档状态。归档后的功能点应只读。"""
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")
    if req.status == "archived":
        raise HTTPException(
            status_code=409,
            detail="需求已归档，功能点已锁定，不可执行此操作。归档后的功能点仅支持查看。",
        )
    return req


@router.post("/{req_id}/confirm", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def confirm_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """确认需求完成——标记为 confirmed（确认完成），允许生成测试用例。

    前置条件：
        - 需求状态为 pending_review（待确认）
        - 用户已确认 SRR 内容、功能点、影响分析无误

    确认后：
        - status → confirmed（确认完成）
        - 只有 confirmed 状态的需求可以触发用例生成

    如果后续需要重新生成，用户可再次提交澄清或重新分析，
    状态会自动重置为 pending_review 或 draft。
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if req.status != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {req.status} 不可确认（仅 pending_review 可确认）",
        )

    # v1.6: 校验功能点审核状态——所有 FP 必须已审核通过
    from app.db.models import FunctionalPoint as FpModel
    fp_stmt = select(FpModel).where(
        FpModel.requirement_id == req_id,
        FpModel.status != "approved",
    )
    fp_result = await session.execute(fp_stmt)
    unapproved_fps = fp_result.scalars().all()
    if unapproved_fps:
        fp_names = [fp.name for fp in unapproved_fps[:5]]
        suffix = "..." if len(unapproved_fps) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail=f"存在 {len(unapproved_fps)} 个未审核的功能点（{', '.join(fp_names)}{suffix}），"
                   f"请先审核所有功能点后再确认",
        )

    # v1.6: 校验影响分析确认状态
    # 若 SRR 中存在 impact_analysis 且包含未确认的受影响 FP，则阻止确认
    srr = req.srr_json or {}
    impact_list = srr.get("impact_analysis", []) or []
    if impact_list:
        unconfirmed_impacts = [item for item in impact_list if not item.get("is_affected")]
        if unconfirmed_impacts:
            fp_names = [item.get("fp_name", "") for item in unconfirmed_impacts[:5]]
            suffix = "..." if len(unconfirmed_impacts) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail=f"存在 {len(unconfirmed_impacts)} 个未确认的影响范围功能点"
                       f"（{', '.join(fp_names)}{suffix}），"
                       f"请先通过影响分析确认功能点后再确认",
            )

    req.status = "confirmed"
    await session.commit()

    logger.info("[API/requirements] 需求确认完成: req_id={}, title={}", req_id, req.title)
    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="confirm_requirement", target_type="requirement", target_id=req_id,
    )
    return ApiResponse(
        success=True,
        data={"req_id": req_id, "status": "confirmed"},
    )


async def _check_no_running_task(req_id: str, exclude_task_id: str | None = None) -> None:
    """检查需求是否有进行中的任务（分析/重生成/FP合并/澄清），如有则拒绝。

    互斥规则：同一需求同时只能有一个 running/pending 的分析类任务。
    - upload_requirement / regenerate / merge_fp / submit_clarify 四种操作互斥
    - 已有任务 running/pending 时抛 HTTPException 409

    Args:
        req_id: 需求 ID
        exclude_task_id: 排除的任务 ID（用于同一个任务的幂等重试场景）

    Raises:
        HTTPException 409: 存在冲突任务
    """
    from sqlalchemy import select as _s, or_ as _or_
    async with async_session() as s:
        stmt = _s(AnalysisTask).where(
            AnalysisTask.req_id == req_id,
            AnalysisTask.status.in_(["pending", "running"]),
            _or_(
                AnalysisTask.task_type == "upload_requirement",
                AnalysisTask.task_type == "regenerate",
                AnalysisTask.task_type == "merge_fp",
                AnalysisTask.task_type == "submit_clarify",
            ),
        )
        if exclude_task_id:
            stmt = stmt.where(AnalysisTask.id != exclude_task_id)
        result = await s.execute(stmt)
        conflict = result.scalar_one_or_none()
        if conflict:
            labels = {
                "upload_requirement": "需求解析",
                "regenerate": "重新生成",
                "merge_fp": "功能点合并",
                "submit_clarify": "澄清重解析",
            }
            label = labels.get(conflict.task_type, conflict.task_type)
            raise HTTPException(
                status_code=409,
                detail=f"需求正在进行「{label}」任务（{conflict.id}），请等待完成后再操作",
            )


@router.get("/{req_id}/fp", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_list(req_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取需求的功能点列表。"""
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    result = await session.execute(
        select(FunctionalPoint).where(
            FunctionalPoint.requirement_id == req_id
        ).order_by(FunctionalPoint.created_at)
    )
    fps = result.scalars().all()
    return ApiResponse(success=True, message="ok", data={
        "items": [
            {
                "id": fp.id, "name": fp.name, "fp_type": fp.fp_type,
                "module_id": fp.module_id, "modules": fp.modules or [],
                "priority": fp.priority, "status": fp.status,
                "srr_json": fp.srr_json, "atomicity_check": fp.atomicity_check,
                "test_hints": fp.test_hints,
                "vectorization_status": fp.vectorization_status,
                "needs_module": not fp.module_id and not (fp.modules or []),
                "created_at": fp.created_at.isoformat() if fp.created_at else "",
            }
            for fp in fps
        ],
        "total": len(fps),
        "is_archived": req.status == "archived",
    })


@router.get("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_dependencies(req_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取功能点依赖关系。

    v2.3: 返回 review_status 字段，前端按审核状态区分颜色。
    """
    fp_ids_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_ids_result.fetchall()}

    # v2.3: 查询以当前需求 FP 为 source 或 target 的所有依赖（支持跨需求依赖查看）
    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id.in_(fp_ids)) | (FPDependency.target_fp_id.in_(fp_ids))
        ).order_by(FPDependency.created_at.desc())
    )
    deps = dep_result.scalars().all()

    # 获取名称映射
    all_ids = {d.source_fp_id for d in deps} | {d.target_fp_id for d in deps}
    name_map = {}
    if all_ids:
        names_result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(all_ids))
        )
        name_map = {row[0]: row[1] for row in names_result.fetchall()}

    return ApiResponse(success=True, message="ok", data={
        "dependencies": [
            {
                "id": d.id, "source_fp_id": d.source_fp_id,
                "source_name": name_map.get(d.source_fp_id, ""),
                "target_fp_id": d.target_fp_id,
                "target_name": name_map.get(d.target_fp_id, ""),
                "relation_type": d.relation_type,
                "description": d.description,
                "review_status": d.review_status,
                "reviewed_by": d.reviewed_by,
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else "",
                "created_by": d.created_by,
                "created_at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in deps
        ]
    })


# ============================================================================
# v2.3: FP 依赖审核流程
# 新建依赖默认 pending 状态，需审核通过后才生效（参与影响分析）
# 支持审核通过/驳回，并记录每次变动历史
# ============================================================================

class FPDependencyReviewRequest(BaseModel):
    """FP 依赖审核请求。"""
    action: str  # approve / reject
    reason: str = ""  # 审核说明（可选）


@router.patch("/{req_id}/fp/dependencies/{dep_id}/review", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_review_dependency(
    req_id: str,
    dep_id: int,
    body: FPDependencyReviewRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """审核功能点依赖关系。

    v2.3 新增。将依赖从 pending 状态审核为 approved 或 rejected。
    - approved: 依赖关系生效，参与影响分析
    - rejected: 依赖关系不生效，但保留记录

    Args:
        req_id: 需求 ID
        dep_id: 依赖 ID
        body: 审核请求，含 action (approve/reject) 和 reason
        session: 数据库会话
        current_user: 当前用户

    Returns:
        ApiResponse: 含更新后的 review_status
    """
    await _load_active_requirement(req_id, session)
    dep = await session.get(FPDependency, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    # 校验 source_fp 属于该 req_id
    source_fp = await session.get(FunctionalPoint, dep.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="依赖关系不属于该需求")

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action 必须为 approve 或 reject")

    if dep.review_status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"依赖关系当前状态为 {dep.review_status}，仅 pending 状态可审核",
        )

    old_status = dep.review_status
    new_status = "approved" if body.action == "approve" else "rejected"
    dep.review_status = new_status
    dep.reviewed_by = current_user.username
    dep.reviewed_at = datetime.now(timezone.utc)

    # 写入历史记录
    session.add(FPDependencyHistory(
        dependency_id=dep.id,
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action=new_status,
        old_review_status=old_status,
        new_review_status=new_status,
        changed_by=current_user.username,
        change_reason=body.reason or f"用户{('审核通过' if new_status == 'approved' else '驳回')}依赖关系",
        related_req_id=req_id,
    ))

    await session.commit()
    logger.info(
        "[FP] 依赖审核: dep_id={}, {} → {}, by={}",
        dep_id, old_status, new_status, current_user.username,
    )

    return ApiResponse(
        success=True,
        message=f"依赖关系已{('审核通过' if new_status == 'approved' else '驳回')}",
        data={
            "id": dep.id,
            "review_status": dep.review_status,
            "reviewed_by": dep.reviewed_by,
            "reviewed_at": dep.reviewed_at.isoformat() if dep.reviewed_at else "",
        },
    )


@router.get("/{req_id}/fp/dependencies/history", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_dependency_history(
    req_id: str,
    dep_id: int | None = Query(default=None, description="可选，按依赖ID筛选"),
    action: str | None = Query(default=None, description="可选，按操作类型筛选 (created/approved/rejected/deleted)"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取功能点依赖关系变动历史。

    v2.3 新增。记录每次依赖关系的创建、审核、修改、删除操作。
    支持按依赖ID、操作类型筛选，关联显示对应需求变更导致的依赖关系变化。

    Args:
        req_id: 需求 ID
        dep_id: 可选，按依赖ID筛选
        action: 可选，按操作类型筛选
        limit: 返回条数上限
        session: 数据库会话

    Returns:
        ApiResponse: data.items 含历史记录列表
    """
    # 查询当前需求下所有 FP ID
    fp_ids_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_ids_result.fetchall()}
    if not fp_ids:
        return ApiResponse(success=True, message="ok", data={"items": [], "total": 0})

    # 查询历史记录（source 或 target 属于当前需求 FP）
    stmt = select(FPDependencyHistory).where(
        (FPDependencyHistory.source_fp_id.in_(fp_ids))
        | (FPDependencyHistory.target_fp_id.in_(fp_ids))
        | (FPDependencyHistory.related_req_id == req_id)
    )
    if dep_id is not None:
        stmt = stmt.where(FPDependencyHistory.dependency_id == dep_id)
    if action:
        stmt = stmt.where(FPDependencyHistory.action == action)

    stmt = stmt.order_by(FPDependencyHistory.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    history_rows = result.scalars().all()

    # 查询 FP 名称映射
    all_fp_ids = set()
    for h in history_rows:
        all_fp_ids.add(h.source_fp_id)
        all_fp_ids.add(h.target_fp_id)
    name_map = {}
    if all_fp_ids:
        names_result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(all_fp_ids))
        )
        name_map = {row[0]: row[1] for row in names_result.fetchall()}

    return ApiResponse(success=True, message="ok", data={
        "items": [
            {
                "id": h.id,
                "dependency_id": h.dependency_id,
                "source_fp_id": h.source_fp_id,
                "source_name": name_map.get(h.source_fp_id, ""),
                "target_fp_id": h.target_fp_id,
                "target_name": name_map.get(h.target_fp_id, ""),
                "relation_type": h.relation_type,
                "description": h.description,
                "action": h.action,
                "old_review_status": h.old_review_status,
                "new_review_status": h.new_review_status,
                "changed_by": h.changed_by,
                "change_reason": h.change_reason,
                "related_req_id": h.related_req_id,
                "created_at": h.created_at.isoformat() if h.created_at else "",
            }
            for h in history_rows
        ],
        "total": len(history_rows),
    })


# FP 依赖合法关系类型（与 FPDependency 模型注释一致）
_FP_RELATION_TYPES = {"depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"}


@router.post("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_add_dependency(
    req_id: str,
    body: FPDependencyCreateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """创建功能点依赖关系。

    v2.3: 新建依赖默认 review_status='pending'，需审核通过后才生效（参与影响分析）。

    Note:
        - source_fp 必须属于该 req_id，且 req 未归档
        - target_fp 可属于同产品下任意需求（支持跨需求依赖）
        - relation_type 必须为 5 种合法值之一
    """
    await _load_active_requirement(req_id, session)

    # 校验关系类型
    if body.relation_type not in _FP_RELATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"relation_type 必须为 {', '.join(sorted(_FP_RELATION_TYPES))} 之一",
        )

    # 不自引用
    if body.source_fp_id == body.target_fp_id:
        raise HTTPException(status_code=400, detail="source 和 target 不能为同一功能点")

    # 校验 source 属于该 req_id
    source_fp = await session.get(FunctionalPoint, body.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="source 功能点不存在或不属于该需求")

    # 校验 target 存在且同产品
    target_fp = await session.get(FunctionalPoint, body.target_fp_id)
    if not target_fp:
        raise HTTPException(status_code=404, detail="target 功能点不存在")

    # 同产品校验
    source_req = await session.get(Requirement, source_fp.requirement_id)
    target_req = await session.get(Requirement, target_fp.requirement_id)
    if not source_req or not target_req or source_req.product_id != target_req.product_id:
        raise HTTPException(status_code=400, detail="source 和 target 功能点必须属于同一产品")

    # 查重
    dup = await session.execute(
        select(FPDependency).where(
            FPDependency.source_fp_id == body.source_fp_id,
            FPDependency.target_fp_id == body.target_fp_id,
            FPDependency.relation_type == body.relation_type,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该依赖关系已存在")

    dep = FPDependency(
        source_fp_id=body.source_fp_id,
        target_fp_id=body.target_fp_id,
        relation_type=body.relation_type,
        description=body.description,
        review_status="pending",  # 默认待审核
        created_by=current_user.username,
    )
    session.add(dep)
    await session.flush()  # 获取 dep.id

    # v2.3: 写入依赖历史记录
    session.add(FPDependencyHistory(
        dependency_id=dep.id,
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action="created",
        old_review_status="",
        new_review_status="pending",
        changed_by=current_user.username,
        change_reason="新建依赖关系",
        related_req_id=req_id,
    ))

    await session.commit()
    await session.refresh(dep)

    logger.info("[FP] 依赖创建(待审核): {} -{}-> {} (req={})",
                body.source_fp_id, body.relation_type, body.target_fp_id, req_id)

    return ApiResponse(
        success=True,
        message="依赖关系已创建（待审核，审核通过后生效）",
        data={
            "id": dep.id, "source_fp_id": dep.source_fp_id,
            "target_fp_id": dep.target_fp_id, "relation_type": dep.relation_type,
            "review_status": dep.review_status,
        },
    )


@router.delete("/{req_id}/fp/dependencies/{dep_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_remove_dependency(
    req_id: str,
    dep_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除功能点依赖关系。归档后不可用。

    v2.3: 删除前写入历史记录。
    """
    await _load_active_requirement(req_id, session)
    dep = await session.get(FPDependency, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    # 校验 source_fp 属于该 req_id
    source_fp = await session.get(FunctionalPoint, dep.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="依赖关系不属于该需求")

    # v2.3: 写入删除历史记录
    session.add(FPDependencyHistory(
        dependency_id=None,  # 删除后置空
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action="deleted",
        old_review_status=dep.review_status,
        new_review_status="",
        changed_by=current_user.username,
        change_reason="用户主动删除依赖关系",
        related_req_id=req_id,
    ))

    await session.delete(dep)
    await session.commit()

    logger.info("[FP] 依赖删除: dep_id={} (req={})", dep_id, req_id)
    return ApiResponse(success=True, message="依赖关系已删除")


# ============================================================================
# v1.6.1: FP 子 SRR 手动编辑保存
# ============================================================================

class FPUpdateRequest(BaseModel):
    name: str | None = None
    module_id: str | None = None
    modules: list[str] | None = None  # v2.2: 多模块关联
    fp_type: str | None = None
    priority: str | None = None
    srr_json: dict | None = None


@router.put("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_update(
    req_id: str,
    fp_id: str,
    body: FPUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """更新功能点名称 / 模块 / 子 SRR。v2.2 支持多模块。

    v2.35: 允许归档状态下编辑功能点，编辑后同步更新所有引用点：
        - 总 SRR fp_index / fp_summary（FP 名称/模块变更时）
        - 向量库 FP 记录（FP 已归档时重新写入 Milvus）
        - 用例 case_json.related_fp（FP 名称变更时批量更新关联用例）
        - FP 依赖关系按 ID 存储，展示层自动读取最新名称，无需额外处理
    """
    # v2.35: 归档状态也允许编辑 FP（不调用 _load_active_requirement）
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")

    # 记录变更前的名称，用于判断是否需要同步用例
    old_name = fp.name
    old_modules = list(fp.modules or [])
    old_srr_json = dict(fp.srr_json) if isinstance(fp.srr_json, dict) else {}
    name_changed = False
    srr_changed = False

    if body.name is not None and body.name != fp.name:
        fp.name = body.name
        name_changed = True
    if body.modules is not None:
        fp.modules = body.modules
    elif body.module_id is not None:
        # 旧版单模块兼容
        fp.modules = [body.module_id]
        fp.module_id = body.module_id
    if body.srr_json is not None:
        fp.srr_json = body.srr_json
        srr_changed = True

    await session.flush()

    # v2.35: 同步更新引用点
    sync_results: list[str] = []

    # 1. FP 名称变更 → 更新关联用例 case_json.related_fp / functional_point
    if name_changed:
        try:
            case_result = await session.execute(
                select(TestCase).where(
                    TestCase.req_id == req_id,
                )
            )
            cases_to_update = case_result.scalars().all()
            updated_count = 0
            for case in cases_to_update:
                case_json = dict(case.case_json) if isinstance(case.case_json, dict) else {}
                related_fp = case_json.get("related_fp", "")
                functional_point = case_json.get("functional_point", "")
                changed = False
                if related_fp == old_name:
                    case_json["related_fp"] = fp.name
                    changed = True
                if functional_point == old_name:
                    case_json["functional_point"] = fp.name
                    changed = True
                if changed:
                    case.case_json = case_json
                    updated_count += 1
            if updated_count > 0:
                sync_results.append(f"用例 related_fp 同步 {updated_count} 条")
                logger.info("[FP] 用例 related_fp 同步: fp_id={}, old_name={}, new_name={}, updated={}",
                            fp_id, old_name, fp.name, updated_count)
        except Exception as e:
            logger.error("[FP] 用例 related_fp 同步失败 fp_id={}: {}", fp_id, str(e)[:200])

    # 2. 更新总 SRR 的 fp_index / fp_summary
    try:
        await _update_fp_index_in_srr(session, req_id)
        sync_results.append("总 SRR fp_index 已更新")
    except Exception as e:
        logger.error("[FP] 总 SRR fp_index 更新失败 req_id={}: {}", req_id, str(e)[:200])

    await session.commit()

    # 3. FP 已归档 → 重新写入向量库（在 commit 后执行，避免事务未提交时写入不一致数据）
    if fp.status == "archived" and (name_changed or srr_changed):
        try:
            from app.db.milvus_store import vector_store

            fp_meta = {
                "type": "functional_point",
                "requirement_id": req_id,
                "fp_id": fp.id or "",
                "fp_name": fp.name or "",
                "modules": ",".join(fp.modules or []) if fp.modules else (fp.module_id or ""),
                "module_id": fp.module_id or "",
                "fp_type": fp.fp_type,
                "product_id": req.product_id,
                "status": "archived",
            }
            embed_srr = dict(fp.srr_json) if isinstance(fp.srr_json, dict) else {}
            embed_srr.setdefault("requirement", {})
            embed_srr["requirement"]["title"] = fp.name
            if fp.modules:
                embed_srr["requirement"]["modules"] = list(fp.modules)
            elif fp.module_id:
                embed_srr["requirement"]["modules"] = [fp.module_id]
            if not embed_srr["requirement"].get("summary"):
                embed_srr["requirement"]["summary"] = (
                    fp.srr_json.get("requirement", {}).get("summary", "")
                    if isinstance(fp.srr_json, dict) else ""
                ) or fp.name

            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=f"FP-{fp.id}",
                srr_json=embed_srr,
                metadata=fp_meta,
            )
            sync_results.append("向量库 FP 记录已更新")
            logger.info("[FP] 向量库 FP 记录已更新: fp_id={}", fp_id)
        except Exception as e:
            logger.warning("[FP] 向量库 FP 记录更新失败 fp_id={}: {}", fp_id, str(e)[:200])

    logger.info("[FP] 更新: fp_id={}, fields={}, sync={}", fp_id,
                [k for k, v in {"name": body.name, "module_id": body.module_id, "srr_json": body.srr_json is not None}.items() if v],
                sync_results)
    return ApiResponse(success=True, message="功能点已更新", data={"id": fp.id, "name": fp.name, "sync": sync_results})


# ============================================================================
# v3.0: FP 影响功能说明（自动生成 + 可编辑，写入 SRR impact_analysis 字段）
# ============================================================================

class ImpactDescriptionUpdateRequest(BaseModel):
    """FP 影响说明编辑保存请求。"""
    description: str
    test_focus: str = ""


# ============================================================================
# v1.7: 需求级影响分析（模块+FP 依赖 + KB + 人工审核）
# ============================================================================

class ImpactConfirmRequest(BaseModel):
    """人工审核确认受影响的 FP 列表。"""
    confirmed_fp_ids: list[str]  # 确认影响的 FP ID 列表
    edited_descriptions: dict[str, str] = {}  # fp_id → 编辑后的影响说明


@router.post("/{req_id}/impact-analysis", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def run_impact_analysis(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """全量影响分析：模块依赖 + FP 依赖 + KB 查询。

    Returns:
        - requirement: 当前需求信息
        - module_impact: 模块级影响
        - affected_fps: 受影响 FP 列表（含 KB 上下文）
        - total_affected: 受影响 FP 总数
    """
    await _load_active_requirement(req_id, session)
    agent = ImpactAnalysisAgent()
    result = await agent.analyze_requirement_impact(req_id, session)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return ApiResponse(success=True, data=result)


@router.post("/{req_id}/impact-analysis/confirm", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def confirm_impact_analysis(
    req_id: str,
    body: ImpactConfirmRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """人工审核确认受影响 FP，写入 SRR impact_analysis。

    操作：
        1. 校验 FP 存在
        2. 将确认的 FP 写入 SRR.impact_analysis，标记 is_affected=true
        3. 已存在的同 fp_id 条目覆盖
    """
    req = await _load_active_requirement(req_id, session)
    srr = req.srr_json or {}

    impact_list = list(srr.get("impact_analysis", []))
    confirmed_fps = []

    for fp_id in body.confirmed_fp_ids:
        fp = await session.get(FunctionalPoint, fp_id)
        if not fp:
            continue
        entry = {
            "fp_id": fp.id,
            "fp_name": fp.name,
            "fp_type": fp.fp_type,
            "module_id": fp.module_id,
            "source_req_id": fp.requirement_id,
            "is_affected": True,
            "impact_description": body.edited_descriptions.get(fp_id, f"受需求[{req.title}]影响"),
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        # 覆盖旧条目
        impact_list = [e for e in impact_list if e.get("fp_id") != fp_id]
        impact_list.append(entry)
        confirmed_fps.append(entry)

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    # 同时标记 FP 自身
    for entry in confirmed_fps:
        fp_in_scope = await session.get(FunctionalPoint, entry["fp_id"])
        if fp_in_scope and fp_in_scope.requirement_id == req_id:
            fp_in_scope.test_hints = fp_in_scope.test_hints or []
            if "回归测试" not in fp_in_scope.test_hints:
                fp_in_scope.test_hints.append("回归测试")

    await session.commit()
    logger.info("[ImpactAnalysis] 确认: req={}, confirmed_fps={}", req_id, len(confirmed_fps))
    return ApiResponse(
        success=True,
        data={"confirmed_count": len(confirmed_fps), "impact_analysis": impact_list},
    )


@router.post("/{req_id}/fp/{fp_id}/impact-description", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_generate_impact_description(
    req_id: str,
    fp_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """自动生成功能点影响说明并写入需求 SRR。

    流程：
        1. 校验 FP 存在且属于该 req_id
        2. 调用 ImpactAnalysisAgent._analyze_fp_impact()（FP 反向 BFS + 模块 BFS + LLM 合成）
        3. 读需求 SRR，追加/覆盖 impact_analysis 数组中同 fp_id 的条目
        4. 写回 DB，返回生成结果

    Args:
        req_id: 需求 ID
        fp_id: 功能点 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含完整的影响分析结果
    """
    # 1. 校验 FP 属于该 req_id
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不属于该需求")

    # 2. 调用 FP 影响分析
    from app.agents.impact_agent import impact_agent

    try:
        impact_result = await impact_agent._analyze_fp_impact(fp_id, session)
    except Exception as e:
        logger.error("[API/requirements] FP 影响分析失败 fp_id={}: {}", fp_id, str(e)[:200])
        raise HTTPException(status_code=500, detail=f"影响分析失败: {str(e)[:200]}")

    # 3. 写入需求 SRR 的 impact_analysis 字段
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    srr = dict(req.srr_json or {})
    impact_list = list(srr.get("impact_analysis", []))

    # 同 fp_id 覆盖旧条目
    impact_list = [item for item in impact_list if item.get("fp_id") != fp_id]
    impact_list.append(impact_result)

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    await session.commit()

    logger.info(
        "[API/requirements] FP 影响说明已生成并写入 SRR: req_id={}, fp_id={}, affected_fps={}, affected_modules={}",
        req_id, fp_id,
        len(impact_result.get("affected_fps", [])),
        len(impact_result.get("affected_modules", [])),
    )

    return ApiResponse(
        success=True,
        message="影响说明已生成并保存到需求 SRR",
        data=impact_result,
    )


@router.put("/{req_id}/impact-description/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_update_impact_description(
    req_id: str,
    fp_id: str,
    body: ImpactDescriptionUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """编辑保存功能点影响说明（用户手动修改后的持久化）。

    Args:
        req_id: 需求 ID
        fp_id: 功能点 ID
        body: 含 description / test_focus
        session: 数据库会话

    Returns:
        ApiResponse: 确认保存成功
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    srr = dict(req.srr_json or {})
    impact_list = list(srr.get("impact_analysis", []))

    # 查找匹配条目
    found = False
    for item in impact_list:
        if item.get("fp_id") == fp_id:
            item["description"] = body.description
            if body.test_focus:
                item["test_focus"] = body.test_focus
            item["is_auto_generated"] = False
            found = True
            break

    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 fp_id={fp_id} 的影响说明条目，请先调用 POST 生成",
        )

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    await session.commit()

    logger.info("[API/requirements] FP 影响说明已更新: req_id={}, fp_id={}", req_id, fp_id)

    return ApiResponse(success=True, message="影响说明已保存")


# ============================================================================
# v1.5.3: FP 合并（多选 → LLM 整理 → 合并为一个 FP）
# ============================================================================

class FPMergeRequest(BaseModel):
    fp_ids: list[str]  # 至少 2 个
    merged_name: str = ""  # 合并后功能点名称（为空则 LLM 自动生成）


@router.post("/{req_id}/fp/merge", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_merge(
    req_id: str,
    body: FPMergeRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """将多个功能点合并为一个（异步任务模式）。

    v2.16 异步化 + 冲突检测：
        1. 校验冲突（不能与其他分析任务同时进行）
        2. 创建 AnalysisTask（task_type=merge_fp）
        3. 后台协程执行 LLM 合并
        4. 立即返回 task_id
    """
    if len(body.fp_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 个功能点才能合并")

    req = await _load_active_requirement(req_id, session)
    await _check_no_running_task(req_id)

    # 加载所有 FP
    fps_to_merge = []
    for fid in body.fp_ids:
        fp = await session.get(FunctionalPoint, fid)
        if not fp or fp.requirement_id != req_id:
            raise HTTPException(status_code=404, detail=f"功能点 {fid} 不存在或不属于该需求")
        fps_to_merge.append(fp)

    if len(fps_to_merge) < 2:
        raise HTTPException(status_code=400, detail="有效功能点不足，无法合并")

    # 创建任务
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="merge_fp",
        req_id=req_id,
        product_id=req.product_id,
        status="pending",
        phase="merge",
        progress=0,
        message=f"正在合并 {len(fps_to_merge)} 个功能点...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    fp_ids = [fp.id for fp in fps_to_merge]
    asyncio.create_task(
        _run_fp_merge_task(
            task_id=task_id, req_id=req_id, fp_ids=fp_ids,
            merged_name=body.merged_name, product_id=req.product_id,
        )
    )

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_merge", target_type="functional_point", target_id=req_id,
        detail={"fp_ids": fp_ids, "merged_name": body.merged_name},
    )

    return ApiResponse(
        success=True,
        message="功能点合并已启动",
        data={"task_id": task_id, "req_id": req_id, "fp_count": len(fp_ids)},
    )


@router.patch("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_update(
    req_id: str, fp_id: str, body: FPUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """编辑功能点（名称/模块/类型/优先级）。

    v2.35: 归档编辑请使用 PUT 接口（含同步更新总 SRR/向量库/用例逻辑）。
    此 PATCH 接口仅做简单字段更新，不包含同步逻辑，归档状态不建议使用。
    """
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if body.name is not None:
        fp.name = body.name
    if body.module_id is not None:
        fp.module_id = body.module_id
    if body.fp_type is not None:
        fp.fp_type = body.fp_type
    if body.priority is not None:
        fp.priority = body.priority
    await session.commit()
    return ApiResponse(success=True, message="功能点已更新")


@router.get("/{req_id}/fp/search-names", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_search_names(
    req_id: str, q: str = Query(min_length=1),
    product_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """搜索已有功能点名称（跨需求复用命名）。"""
    stmt = select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.requirement_id).where(
        FunctionalPoint.name.ilike(f"%{q}%")
    ).order_by(FunctionalPoint.created_at.desc()).limit(20)

    if product_id:
        req_ids_result = await session.execute(
            select(Requirement.id).where(Requirement.product_id == product_id)
        )
        req_ids = {r[0] for r in req_ids_result.fetchall()}
        if req_ids:
            stmt = stmt.where(FunctionalPoint.requirement_id.in_(req_ids))

    result = await session.execute(stmt)
    rows = result.fetchall()
    return ApiResponse(success=True, message="ok", data={
        "names": [{"id": r[0], "name": r[1], "req_id": r[2]} for r in rows],
    })


# ============================================================================
# v2.3: 功能点复用机制
# 需求归档后 FP 进入 archived 状态，可被新需求复用，避免重复生成
# 复用时继承原始模块信息，无需用户手动选择
# ============================================================================

@router.get("/{req_id}/fp/reusable", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_list_reusable(
    req_id: str,
    module_id: str | None = Query(default=None, description="按模块筛选已归档的可复用 FP"),
    q: str | None = Query(default=None, description="按名称模糊搜索"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出可复用的已归档功能点。

    v2.3 新增。需求归档后，所属 FP 进入 archived 状态，可被新需求复用。
    返回同产品下所有 status='archived' 的 FP，支持按模块、名称筛选。

    Args:
        req_id: 当前需求 ID（用于确定产品范围）
        module_id: 可选，按模块筛选
        q: 可选，按名称模糊搜索
        session: 数据库会话

    Returns:
        ApiResponse: data.items 含可复用 FP 列表，含源需求信息
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    # 查询同产品下所有已归档 FP（排除当前需求自身的 FP）
    stmt = (
        select(FunctionalPoint, Requirement.title)
        .join(Requirement, FunctionalPoint.requirement_id == Requirement.id)
        .where(
            FunctionalPoint.status == "archived",
            Requirement.product_id == req.product_id,
            FunctionalPoint.requirement_id != req_id,
        )
        .order_by(FunctionalPoint.updated_at.desc())
    )

    if module_id:
        stmt = stmt.where(
            (FunctionalPoint.module_id == module_id)
            | FunctionalPoint.modules.contains([module_id])
        )

    if q:
        stmt = stmt.where(FunctionalPoint.name.ilike(f"%{q}%"))

    stmt = stmt.limit(100)
    result = await session.execute(stmt)
    rows = result.all()

    # 查询模块名称映射
    all_module_ids: set[str] = set()
    for fp, _ in rows:
        if fp.module_id:
            all_module_ids.add(fp.module_id)
        for m in (fp.modules or []):
            all_module_ids.add(m)

    module_names: dict[str, str] = {}
    if all_module_ids:
        from app.db.models import Module as ModuleModel
        mod_result = await session.execute(
            select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_module_ids))
        )
        module_names = {row[0]: row[1] for row in mod_result.fetchall()}

    items = []
    for fp, src_req_title in rows:
        items.append({
            "id": fp.id,
            "name": fp.name,
            "fp_type": fp.fp_type,
            "priority": fp.priority,
            "module_id": fp.module_id,
            "modules": fp.modules or [],
            "module_names": [module_names.get(m, m) for m in (fp.modules or ([fp.module_id] if fp.module_id else []))],
            "source_req_id": fp.requirement_id,
            "source_req_title": src_req_title,
            "test_hints": fp.test_hints or [],
            "archived_at": fp.updated_at.isoformat() if fp.updated_at else "",
        })

    return ApiResponse(
        success=True,
        message=f"找到 {len(items)} 个可复用功能点",
        data={"items": items, "total": len(items)},
    )


class FPReuseRequest(BaseModel):
    """功能点复用请求。"""
    source_fp_id: str  # 源 FP ID（已归档）
    name: str | None = None  # 复用后的新名称（可选，默认继承源 FP 名称）


@router.post("/{req_id}/fp/reuse", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_reuse(
    req_id: str,
    body: FPReuseRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """复用已归档的功能点。

    v2.3 新增。从同产品下已归档的需求中复用功能点：
    - 复制源 FP 的 name / fp_type / srr_json / atomicity_check / test_hints
    - 继承源 FP 的 module_id 和 modules（无需用户手动选择）
    - 新 FP 状态设为 pending_review（在新需求上下文下需重新审核）
    - 关联到当前需求（requirement_id = req_id）

    Args:
        req_id: 目标需求 ID
        body: 复用请求，含源 FP ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含新 FP 信息
    """
    await _load_active_requirement(req_id, session)

    # 查询源 FP
    source_fp = await session.get(FunctionalPoint, body.source_fp_id)
    if not source_fp:
        raise HTTPException(status_code=404, detail="源功能点不存在")
    if source_fp.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"源功能点「{source_fp.name}」未归档（当前状态: {source_fp.status}），不可复用",
        )

    # 同产品校验
    source_req = await session.get(Requirement, source_fp.requirement_id)
    target_req = await session.get(Requirement, req_id)
    if not source_req or not target_req:
        raise HTTPException(status_code=404, detail="源需求或目标需求不存在")
    if source_req.product_id != target_req.product_id:
        raise HTTPException(status_code=400, detail="源功能点与目标需求必须属于同一产品")

    # 查重 — 同需求下不能复用同名 FP
    new_name = (body.name or source_fp.name).strip()[:255]
    if not new_name:
        raise HTTPException(status_code=400, detail="功能点名称不能为空")

    dup = await session.execute(
        select(FunctionalPoint.id).where(
            FunctionalPoint.requirement_id == req_id,
            FunctionalPoint.name == new_name,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"当前需求已存在同名功能点「{new_name}」，请修改名称后重试",
        )

    # 创建新 FP — 继承源 FP 的模块信息
    new_fp = FunctionalPoint(
        id=gen_requirement_id(),
        requirement_id=req_id,
        module_id=source_fp.module_id,  # 继承源 FP 模块
        modules=list(source_fp.modules or []),  # 继承源 FP 多模块关联
        name=new_name,
        fp_type=source_fp.fp_type,
        priority=source_fp.priority,
        srr_json=dict(source_fp.srr_json) if isinstance(source_fp.srr_json, dict) else {},
        atomicity_check=dict(source_fp.atomicity_check) if isinstance(source_fp.atomicity_check, dict) else {},
        test_hints=list(source_fp.test_hints or []),
        status="pending_review",  # 复用后需在新需求上下文下重新审核
    )
    session.add(new_fp)
    await session.commit()
    await session.refresh(new_fp)

    logger.info(
        "[FP] 复用成功: source_fp_id={} → new_fp_id={}, name={}, req_id={}, module_id={}",
        source_fp.id, new_fp.id, new_fp.name, req_id, new_fp.module_id,
    )

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_reuse", target_type="functional_point", target_id=new_fp.id,
        detail={"source_fp_id": source_fp.id, "source_req_id": source_fp.requirement_id},
    )

    return ApiResponse(
        success=True,
        message=f"功能点「{new_fp.name}」已复用，模块已继承",
        data={
            "id": new_fp.id,
            "name": new_fp.name,
            "fp_type": new_fp.fp_type,
            "module_id": new_fp.module_id,
            "modules": new_fp.modules or [],
            "status": new_fp.status,
            "source_fp_id": source_fp.id,
            "source_req_id": source_fp.requirement_id,
        },
    )


@router.delete("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_delete(
    req_id: str,
    fp_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除功能点。归档后不可用。"""
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    # 删除关联依赖
    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id == fp_id) | (FPDependency.target_fp_id == fp_id)
        )
    )
    for dep in dep_result.scalars().all():
        await session.delete(dep)
    await session.delete(fp)
    await session.commit()
    # v2.22: 删除 FP 后更新总 SRR 的 fp_index 和 fp_summary
    try:
        await _update_fp_index_in_srr(session, req_id)
        await session.commit()
    except Exception as e:
        logger.warning("[FP-Delete] 更新 fp_index/fp_summary 失败（不阻断）: {}", str(e)[:200])
    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_delete", target_type="functional_point", target_id=fp_id,
        detail={"req_id": req_id},
    )
    return ApiResponse(success=True, message="功能点已删除")


# ============================================================================
# v1.5.1: FP 审核 + AI 重生成
# ============================================================================

class FPRegenerateRequest(BaseModel):
    """单 FP AI 重生成请求。"""
    hint: str = ""  # 用户补充说明（可选，引导 LLM 调整内容）


@router.patch("/{req_id}/fp/{fp_id}/approve", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_approve(
    req_id: str, fp_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """审核通过单个功能点。归档后不可用。

    v2.3: 强制要求功能点必须分配模块才能审核通过。
    """
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if fp.status == "approved":
        return ApiResponse(success=True, message="该功能点已审核通过")
    # v2.3: 模块校验 — 未分配模块的功能点不允许审核通过
    if not fp.module_id and not (fp.modules or []):
        raise HTTPException(
            status_code=409,
            detail=f"功能点「{fp.name}」未分配模块，请先编辑该功能点并选择对应模块后再审核通过",
        )
    fp.status = "approved"
    await session.commit()
    logger.info("[FP] 审核通过: fp_id={}, name={}", fp_id, fp.name)
    return ApiResponse(success=True, message=f"功能点「{fp.name}」已审核通过")


@router.patch("/{req_id}/fp/{fp_id}/regenerate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_regenerate(
    req_id: str, fp_id: str,
    body: FPRegenerateRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """AI 重新生成单个功能点的子 SRR。可传入 hint 引导生成方向。归档后不可用。"""
    req = await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")

    hint = body.hint if body and body.hint else ""
    srr = req.srr_json or {}
    raw_text = req.raw_text or ""

    from app.core import prompt_manager as pm2
    regenerate_prompt = f"""重新生成以下功能点的子 SRR。

原始需求：{srr.get("requirement", {}).get("title", req.title)}
功能点名称：{fp.name}
当前功能点类型：{fp.fp_type}
{f'用户补充说明：{hint}' if hint else ''}

原始需求文档（参考）：
{raw_text[:8000]}

请严格按照一体式 FP 拆分的输出格式，只输出这一个功能点的完整 JSON（含 sub_srr）。"""

    try:
        result = await llm_client.call_json(
            prompt=regenerate_prompt,
            system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
            schema_hint={"name": str, "fp_type": str, "summary": str, "sub_srr": dict, "atomicity_check": dict, "test_hints": list},
            temperature=0.2,
            agent_name="FpAgent",
        )
        sub_srr = result.get("sub_srr", {}) if isinstance(result, dict) else {}
        fp.srr_json = {
            **sub_srr,
            "summary": result.get("summary", "") if isinstance(result, dict) else "",
            "suggested_module": result.get("suggested_module", "") if isinstance(result, dict) else "",
        }
        if isinstance(result, dict):
            fp.atomicity_check = result.get("atomicity_check", fp.atomicity_check)
            fp.test_hints = result.get("test_hints", fp.test_hints)
            if result.get("name") and result["name"] != fp.name:
                fp.name = result["name"][:255]
            if result.get("fp_type"):
                fp.fp_type = result["fp_type"]
        fp.status = "pending_review"  # 重生成后需重新审核
        await session.commit()
        return ApiResponse(
            success=True,
            message=f"功能点「{fp.name}」已重新生成，待审核",
            data={"name": fp.name, "fp_type": fp.fp_type, "srr_json": fp.srr_json, "test_hints": fp.test_hints},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重生成失败: {str(e)[:200]}")


# ============================================================================
# 17. 需求 SRR → XMind 导出
# ============================================================================

@router.get("/{req_id}/export/xmind", dependencies=[Depends(get_current_user)])
async def export_requirement_xmind(
    req_id: str,
    format: str = "xmind",
    session: AsyncSession = Depends(get_session),
) -> Response:
    """导出需求 SRR 为 XMind 思维导图（.xmind ZIP 文件）。

    支持两种 XMind 格式（与测试用例导出保持一致）：
    - ``xmind``（默认）：XMind 2020+ / Zen / 2022+ JSON 格式（content.json）
    - ``xmind8``：XMind 8 兼容的 XMAP 2.0 XML 格式（content.xml）

    树结构：
        需求标题（根）
          ├── 总体SRR（一级）
          │   ├── 基本信息（优先级/类型/摘要）
          │   ├── 主流程（Happy Path 步骤）
          │   ├── 异常分支
          │   ├── 业务规则 / 边界条件 / 数据约束 / 验收标准 / 前置条件
          │   └── 附加详情
          ├── [fp_type] 功能点1（一级）
          │   ├── 摘要 / 主流程 / 异常分支 / 业务规则 / ...
          │   └── 测试提示
          └── [fp_type] 功能点2（一级）

    Args:
        req_id: 需求 ID
        format: 导出格式，``xmind``（JSON）或 ``xmind8``（XML）
        session: 数据库会话

    Returns:
        Response: .xmind 文件（application/zip）
    """
    fmt = (format or "xmind").strip().lower()
    if fmt not in ("xmind", "xmind8"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {format}，支持: xmind / xmind8")

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    srr = record.srr_json or {}
    if not isinstance(srr, dict):
        srr = {}

    # 查询功能点列表，合并到 srr 中供导出器使用（srr_json 本身不含 FP 详细数据）
    from sqlalchemy import select as sa_select
    fp_result = await session.execute(
        sa_select(
            FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
            FunctionalPoint.priority, FunctionalPoint.srr_json, FunctionalPoint.test_hints,
            FunctionalPoint.status,
        )
        .where(FunctionalPoint.requirement_id == req_id)
        .order_by(FunctionalPoint.created_at)
    )
    fp_rows = fp_result.all()
    if fp_rows:
        srr = dict(srr)  # 浅拷贝避免修改 ORM 持有对象
        srr["functional_points"] = [
            {
                "fp_id": row[0],
                "name": row[1],
                "fp_type": row[2],
                "priority": row[3],
                "srr_json": row[4] or {},
                "test_hints": row[5] or [],
                "status": row[6],
            }
            for row in fp_rows
        ]

    # 安全文件名（去除路径分隔符等特殊字符）
    raw_title = (srr.get("requirement") or {}).get("title") if isinstance(srr.get("requirement"), dict) else record.title
    raw_title = str(raw_title or req_id)
    safe_title = "".join(c for c in raw_title if c not in '\\/:*?"<>|\r\n\t').strip()[:60] or req_id

    try:
        if fmt == "xmind8":
            from app.exporters.req_xmind import export_requirement_to_xmind
            content = export_requirement_to_xmind(srr, req_title=record.title or "", req_id=req_id)
        else:
            from app.exporters.req_xmind import export_requirement_to_xmind_json
            content = export_requirement_to_xmind_json(srr, req_title=record.title or "", req_id=req_id)
    except Exception as e:
        logger.exception("[API/requirements] XMind 导出失败: req_id={}, format={}", req_id, fmt)
        raise HTTPException(status_code=500, detail=f"XMind 导出失败: {str(e)[:200]}")

    # 文件名后缀区分格式（与测试用例导出一致）
    suffix = "-xmind8" if fmt == "xmind8" else ""
    filename = f"{safe_title}_{req_id}{suffix}.xmind"
    # RFC 5987: 中文文件名需用 filename* 编码（Starlette header 用 latin-1，不支持中文）
    from urllib.parse import quote
    filename_ascii = f"requirement_{req_id}{suffix}.xmind"  # ASCII fallback
    filename_utf8 = quote(filename, safe="")  # URL 编码后的中文文件名
    logger.info("[API/requirements] XMind 导出: req_id={}, format={}, size={} bytes", req_id, fmt, len(content))

    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{filename_utf8}",
            "Content-Length": str(len(content)),
        },
    )
