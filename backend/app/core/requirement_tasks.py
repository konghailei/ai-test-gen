from __future__ import annotations

from datetime import datetime, timezone, date
import asyncio
import json
import re
import base64
from typing import Any

from sqlalchemy import func, select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.agents.clarify_agent import clarify_agent
from app.agents.impact_agent import ImpactAnalysisAgent
from app.utils.id_gen import gen_requirement_id, gen_task_id
from app.agents.req_agent import req_agent
from app.db.database import async_session
from app.db.models import Requirement, Project, AnalysisTask, FunctionalPoint, FPDependency
from app.db.models import TestCase, GenerationBatch, ReqDependency, Traceability
from app.db.models import ErrorCase, FPDependencyHistory
from app.utils.logger import logger



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


# v3.1.3 增强（跨需求依赖）：FP 引用名称归一化。
# LLM 在 cross_fp_flows / fp_dependencies 中引用历史功能点时常带版本后缀
# （"权限控制（v1.0）" / "[v1.0]"），而已归档 FP 的 name 是纯名称（"权限控制"），
# 精确匹配必然落空，导致跨需求依赖边永远无法自动生成。此函数剥离尾部版本标注。
_FP_REF_VERSION_RE = re.compile(r"[\[（(]\s*v?\s*\d+[\w.\-]*\s*[\]）)]\s*$")

def _normalize_fp_ref(name: str) -> str:
    """去除 FP 引用名称尾部的版本标注（[v1.0]/（v1.0）等），返回纯名称。

    Args:
        name: LLM 提取的 FP 引用名称

    Returns:
        str: 纯名称（无版本后缀）；空输入返回空字符串
    """
    if not name:
        return ""
    cleaned = _FP_REF_VERSION_RE.sub("", name.strip())
    return (cleaned or name).strip()

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

def _count_clarify_rounds_since_last_regen(clarify_history: list) -> int:
    """统计最近一次重新生成（regenerate）之后的澄清轮次。

    v2.42: 3轮收敛应基于"当前重新生成周期"内的澄清次数，而非累计历史。
    重新生成产生全新 SRR，属于新一轮需求分析，应重置澄清轮次。
    若历史中无 regenerate 记录，则统计所有 clarify 记录（首次需求分析场景）。

    Args:
        clarify_history: _clarify_history 列表

    Returns:
        int: 当前周期内已完成的澄清轮次
    """
    if not clarify_history:
        return 0
    last_regen_idx = -1
    for i, h in enumerate(clarify_history):
        if isinstance(h, dict) and h.get("action") == "regenerate":
            last_regen_idx = i
    scope = clarify_history[last_regen_idx + 1:] if last_regen_idx >= 0 else clarify_history
    return sum(1 for h in scope if isinstance(h, dict) and h.get("action") == "clarify")

def _read_fp_extract_degrade(src: dict | None) -> tuple[bool, str]:
    """读取 FP 提取降级标记（v3.4.6 / 缺陷 B）。

    标记由 `ReqAgent._fp_extract_from_raw` 打在**传入的 SRR 字典**上
    （`_fp_extract_degraded` / `_fp_extract_error`）。

    ⚠️ 为什么要在调用点立即捕获、而不是在出口读 srr_json_data：
    主入口出口的 `srr_json_data` 可能已被替换为 ClarifyAgent 的
    `augmented_srr`（一个**新字典**），其上不会带 ReqAgent 打的标记；
    同理 `final_data = dict(final_srr)` 是浅拷贝，标记虽能带上但语义脆弱。
    在调用点用局部变量捕获，是唯一不会被中间层覆盖的做法。

    Returns:
        (是否降级, 失败原因)
    """
    if not isinstance(src, dict):
        return False, ""
    return bool(src.get("_fp_extract_degraded")), str(src.get("_fp_extract_error", "") or "")


_FP_DEGRADE_REASON_OK = (
    "功能点提取失败（LLM 不可用或返回异常），本次为 0 个功能点"
    "——这不是「需求没有功能点」，请检查 LLM 供应商配置后重新分析"
)
_FP_DEGRADE_REASON_FAILED = (
    "功能点提取失败（LLM 不可用或返回异常），本次任务失败可能与之相关"
)


def _apply_fp_extract_degrade(
    target: dict,
    degraded: bool,
    error: str,
    *,
    reason: str = _FP_DEGRADE_REASON_OK,
    overwrite: bool = False,
) -> bool:
    """把 FP 提取降级标记写入结果字典（v3.4.6 / 缺陷 B 的统一出口）。

    ⚠️ 为什么必须抽成单一函数（而不是 6 处各写一遍）：
    本标记需要在 **3 个入口 × 2 条路径（completed / failed）共 6 处**透出。
    首版实现只覆盖了 completed 路径，两个 reparse 入口的 `except` 分支整体漏写
    ——项目里同类事故已发生多次（「打标记类补丁只覆盖 happy path」）。
    收敛到同一函数后，「失败路径漏标」变成对**同一函数调用完整性**的检查，
    而不是对 6 段相似代码的人工逐段比对；也让守卫断言有唯一落点。

    Args:
        target: 承载标记的字典（result_json / resp_data）
        degraded: 是否降级
        error: 失败原因（可为空）
        reason: degraded_reason 文案前缀
        overwrite: 若已有 degraded 标记是否覆盖其 reason（默认保留更早、更具体的原因）

    Returns:
        是否实际写入了标记
    """
    if not degraded or not isinstance(target, dict):
        return False
    target["fp_extract_degraded"] = True
    target["fp_extract_error"] = error
    if overwrite or not target.get("degraded"):
        target["degraded"] = True
        target["degraded_reason"] = reason + (f"（原因：{error}）" if error else "")
    return True


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
    merged = dict(base_srr)

    # v2.8.52: 深拷贝嵌套结构，避免浅拷贝副作用污染调用方数据
    import copy as _copy
    cfi = _copy.deepcopy(merged.get("cross_fp_interactions"))
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
            seen = {json.dumps(_normalize_cross_fp_flow(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_cross_fp_flow(item)
                if not norm.get("source_fp") and not norm.get("target_fp"):
                    continue
                sig = json.dumps(norm, ensure_ascii=False, sort_keys=True)
                if sig not in seen:
                    existing.append(norm)
                    seen.add(sig)

        elif key == "shared_business_rules":
            existing = cfi["shared_business_rules"]
            seen = {json.dumps(_normalize_shared_business_rule(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_shared_business_rule(item)
                if not norm.get("rule"):
                    continue
                sig = json.dumps(norm, ensure_ascii=False, sort_keys=True)
                if sig not in seen:
                    existing.append(norm)
                    seen.add(sig)

        elif key == "fp_contracts":
            existing = cfi["fp_contracts"]
            seen = {json.dumps(_normalize_fp_contract(x), ensure_ascii=False, sort_keys=True)
                    for x in existing if isinstance(x, dict)}
            for item in value:
                norm = _normalize_fp_contract(item)
                if not norm.get("provider_fp") and not norm.get("consumer_fp"):
                    continue
                sig = json.dumps(norm, ensure_ascii=False, sort_keys=True)
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
    product_id: str = "",
) -> int:
    """将 ReqAgent 提取的功能点列表存入 functional_points 表。

    v2.8.55 修复 Bug A-1：解析 suggested_module（模块名称）-> Module.id（FK），
    写入 module_id + modules JSON 列，避免 FP 入库后模块信息丢失导致：
      - FP 复用候选查询命中 0 条
      - 依赖图谱模块筛选失效
      - FP 影响分析跳过模块级 BFS
      - 归档向量 modules 元数据为空
    每个 FP 的 sub_srr -> srr_json，suggested_module 解析后仍保留在 srr_json 中。
    返回实际入库的 FP 数量。

    v2.8.56：新增 FP 拆分质量门禁（FPGate），落库前校验 FP 完整性。
           门禁失败抛出 ValueError，由调用方捕获并中断流水线。
    """
    if not fps:
        return 0

    # v4.0.3 修复（自证 T2）：FP 字段归一化——补齐软性缺失字段，避免门禁误熔断。
    # 背景：澄清重生成路径下 LLM 输出格式漂移，偶尔缺失 `summary` 字段（仅字段缺失、
    # 内容可推导），而 FPGate 把「summary 字段不存在」判为 Blocker 直接中断整条流水线，
    # 导致 12 个澄清答案白答、需求卡在 draft。summary 属「可自动补全的软性缺失」，
    # 不应是 Blocker。此处统一归一化：name 缺失→用占位；summary 缺失/空→用 name 兜底；
    # 保证门禁只拦真错误（空列表/重名）。
    for _fp in fps:
        if isinstance(_fp, dict):
            if "summary" not in _fp or not str(_fp.get("summary") or "").strip():
                _fp["summary"] = str(_fp.get("name") or "").strip()
            if "name" not in _fp:
                _fp["name"] = ""

    # v2.8.56: FP 拆分质量门禁检查
    from app.core.fp_gate import fp_gate
    gate_result = fp_gate.check(fps, srr=None)
    if not gate_result.passed:
        blocker_msg = "; ".join(gate_result.blockers)
        logger.error(
            "[FP-AutoSave] FP 拆分质量门禁未通过（中断流水线）: req_id={}, blockers={}",
            req_id, blocker_msg,
        )
        raise ValueError(f"FP 拆分质量门禁未通过: {blocker_msg}")
    if gate_result.warnings:
        logger.warning(
            "[FP-AutoSave] FP 拆分质量门禁警告: req_id={}, warnings={}",
            req_id, "; ".join(gate_result.warnings),
        )

    saved = 0
    # v2.48: 数据类型中文化兜底（防止 LLM 输出英文类型）
    from app.exporters.req_xmind import _data_type_cn

    # v2.8.55 Bug A-1：预加载该产品下的 Module 表，构建 name -> id 映射
    module_name_to_id: dict[str, str] = {}
    if product_id:
        try:
            from app.db.models import Module as _ModuleModel
            mod_rows = await session.execute(
                select(_ModuleModel.id, _ModuleModel.name).where(
                    _ModuleModel.product_id == product_id
                )
            )
            for mid, mname in mod_rows.all():
                if mname:
                    module_name_to_id[mname.strip()] = mid
                    module_name_to_id[mname.strip().lower()] = mid
        except Exception as mod_e:
            logger.warning(
                "[FP-AutoSave] 预加载 Module 映射失败（FP module_id 将留空）: product_id={}, err={}",
                product_id, str(mod_e)[:150],
            )

    def _resolve_module_id(suggested: str) -> str | None:
        """suggested_module 名称 -> Module.id，支持精确/忽略大小写匹配。"""
        if not suggested or not module_name_to_id:
            return None
        s = suggested.strip()
        if not s:
            return None
        if s in module_name_to_id:
            return module_name_to_id[s]
        return module_name_to_id.get(s.lower())

    for fp in fps:
        fp_id = gen_requirement_id()
        sub_srr = fp.get("sub_srr", {}) if isinstance(fp, dict) else {}
        # v2.48: 对 sub_srr 中的 data_constraints.type 做中文化兜底
        if isinstance(sub_srr, dict):
            for dc in sub_srr.get("data_constraints", []) or []:
                if isinstance(dc, dict) and dc.get("type"):
                    dc["type"] = _data_type_cn(str(dc["type"]))

        # v2.8.55 Bug A-1：解析 suggested_module -> module_id
        suggested_module = (
            fp.get("suggested_module", "") if isinstance(fp, dict) else ""
        )
        resolved_module_id = _resolve_module_id(suggested_module)
        modules_list: list[str] = [resolved_module_id] if resolved_module_id else []

        record = FunctionalPoint(
            id=fp_id,
            requirement_id=req_id,
            module_id=resolved_module_id,
            modules=modules_list,
            name=((fp.get("name") or "未命名功能点") if isinstance(fp, dict) else "未命名功能点")[:255],
            fp_type=(
                fp.get("fp_type", "workflow") if isinstance(fp, dict) else "workflow"
            ),
            priority=priority,
            srr_json={
                **sub_srr,
                "summary": sub_srr.get("summary") or (fp.get("summary", "") if isinstance(fp, dict) else ""),
                "suggested_module": suggested_module,
            },
            atomicity_check=(
                fp.get("atomicity_check", {}) if isinstance(fp, dict) else {}
            ),
            test_hints=[],
            status="pending_review",
        )
        session.add(record)
        saved += 1

    await session.flush()
    logger.info(
        "[FP-AutoSave] 需求 {} 自动存入 {} 个功能点 (product_id={}, module 映射表大小={})",
        req_id, saved, product_id, len(module_name_to_id),
    )
    return saved

async def _register_reused_fps(
    session: AsyncSession, req_id: str, reused_fps: list[dict], priority: str = "P2",
) -> int:
    """将 LLM 标记 reuse_existing 的功能点登记为「复用副本」落库（v3.5.7，DEF-1 修复 P1-A）。

    原缺陷：`_fp_extract_from_raw` 把 reuse_existing 项过滤后直接丢弃、不落库，
    导致「全复用场景下需求 FP 恒为 0」（docs/24 实证复现 3 次）。

    语义对齐 POST /requirements/{rid}/fp/reuse 端点（v2.3）：
      - 源 FP 必须已归档（复用 = 从基线拷贝）；
      - 复制 name / fp_type / srr_json / atomicity_check / test_hints，继承模块；
      - 新 FP 状态 pending_review（在新需求上下文下重新审核）；
      - 同需求下查重（同名已存在则跳过该副本）。

    额外动作（对齐 docs/25 方案 §2.3）：
      - srr_json 写入 _reuse_source 追溯信息（源 FP / 源需求 / 归档时间）；
      - 建立跨版本依赖边：新 FP --depends_on--> 源 FP（pending，需求确认时自动审核），
        让「改基线 → 波及复用方」进入影响分析，同时给 CRSR 聚合制造同名输入。

    Returns:
        int: 实际登记的副本数量
    """
    if not reused_fps:
        return 0

    saved = 0
    for item in reused_fps:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        source_fp_id = item.get("source_fp_id") or ""
        if not name or not source_fp_id:
            logger.warning(
                "[FP-Reuse] 复用副本信息不完整，跳过: req_id={}, name={!r}, source={!r}",
                req_id, name, source_fp_id,
            )
            continue

        source_fp = await session.get(FunctionalPoint, source_fp_id)
        if not source_fp:
            logger.warning(
                "[FP-Reuse] 源 FP 不存在（候选已失效），跳过: req_id={}, source_fp_id={}",
                req_id, source_fp_id,
            )
            continue
        if source_fp.status != "archived":
            # 与 /fp/reuse 语义对齐：源未归档不构成复用基线 → 丢弃（不静默）
            logger.warning(
                "[FP-Reuse] 源 FP 未归档（status={}），按新建处理并丢弃复用副本: req_id={}, fp={}",
                source_fp.status, req_id, name,
            )
            continue

        # 同需求查重（uq_fp_req_name 唯一约束保护 + 与新建 FP 撞名时复用副本优先跳过重复项）
        dup = await session.execute(
            select(FunctionalPoint.id).where(
                FunctionalPoint.requirement_id == req_id,
                FunctionalPoint.name == name,
            )
        )
        if dup.scalar_one_or_none():
            logger.info("[FP-Reuse] 同需求已存在同名 FP，跳过副本: req_id={}, name={}", req_id, name)
            continue

        source_req = await session.get(Requirement, source_fp.requirement_id)
        new_fp = FunctionalPoint(
            id=gen_requirement_id(),
            requirement_id=req_id,
            module_id=source_fp.module_id,
            modules=list(source_fp.modules or []),
            name=name,
            fp_type=item.get("fp_type") or source_fp.fp_type,
            priority=priority,
            srr_json={
                **(source_fp.srr_json if isinstance(source_fp.srr_json, dict) else {}),
                "_reuse_source": {
                    "fp_id": source_fp.id,
                    "req_id": source_fp.requirement_id,
                    "req_title": (source_req.title if source_req else ""),
                    "archived_at": (
                        source_req.archived_at.isoformat()
                        if source_req and getattr(source_req, "archived_at", None) else ""
                    ),
                },
            },
            atomicity_check=(
                dict(source_fp.atomicity_check)
                if isinstance(source_fp.atomicity_check, dict) else {}
            ),
            test_hints=list(source_fp.test_hints or []),
            status="pending_review",
        )
        session.add(new_fp)
        await session.flush()  # 拿到 new_fp.id 供依赖边使用

        # 跨版本依赖边：新 FP depends_on 源 FP（pending，confirm 时随本需求自动审核）
        try:
            dep_dup = await session.execute(
                select(FPDependency).where(
                    FPDependency.source_fp_id == new_fp.id,
                    FPDependency.target_fp_id == source_fp.id,
                    FPDependency.relation_type == "depends_on",
                )
            )
            if not dep_dup.scalar_one_or_none():
                session.add(FPDependency(
                    source_fp_id=new_fp.id,
                    target_fp_id=source_fp.id,
                    relation_type="depends_on",
                    description=f"跨版本复用基线（源需求：{source_req.title if source_req else source_fp.requirement_id}）",
                ))
        except Exception as dep_e:
            logger.warning(
                "[FP-Reuse] 复用追溯依赖边创建失败（不阻断）: req_id={}, fp={}, err={}",
                req_id, name, str(dep_e)[:150],
            )

        saved += 1
        logger.info(
            "[FP-Reuse] 复用副本已登记: req_id={}, name={}, source_fp_id={}, source_req_id={}",
            req_id, name, source_fp.id, source_fp.requirement_id,
        )

    if saved:
        logger.info("[FP-Reuse] 需求 {} 共登记 {} 个复用副本", req_id, saved)
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
        new_flows = []
        for flow in flows:
            if not isinstance(flow, dict):
                new_flows.append(flow)
                continue
            for key in ("source_fp", "target_fp"):
                val = flow.get(key, "")
                if val in old_names:
                    flow[key] = new_name
                    changed = True
            # v2.45 修复：过滤自引用流程（合并后 source_fp == target_fp 无意义）
            if flow.get("source_fp") != flow.get("target_fp"):
                new_flows.append(flow)
            else:
                changed = True  # 标记变更以触发持久化
                logger.info(
                    "[FP-Merge] 过滤自引用 cross_fp_flow: req_id={}, flow={}",
                    req_id, flow,
                )
        if len(new_flows) != len(flows):
            cfi["cross_fp_flows"] = new_flows
        flows = new_flows

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
        new_contracts = []
        for contract in contracts:
            if not isinstance(contract, dict):
                new_contracts.append(contract)
                continue
            for key in ("provider_fp", "consumer_fp"):
                val = contract.get(key, "")
                if val in old_names:
                    contract[key] = new_name
                    changed = True
            # v2.45 修复：过滤自引用契约（合并后 provider_fp == consumer_fp 无意义）
            if contract.get("provider_fp") != contract.get("consumer_fp"):
                new_contracts.append(contract)
            else:
                changed = True
                logger.info(
                    "[FP-Merge] 过滤自引用 fp_contract: req_id={}, contract={}",
                    req_id, contract,
                )
        if len(new_contracts) != len(contracts):
            cfi["fp_contracts"] = new_contracts

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

    v3.1.3 增强（跨需求依赖）：
        原实现只在本需求 FP 内解析名称，LLM 提取到跨需求功能点引用时
        （如权限回收 -> 权限控制[v1.0]）解析不到即静默跳过，导致跨需求
        依赖边永远无法自动生成。现扩展两个解析层级：
          1. 本需求 FP（优先，语义最精确）
          2. 同产品下其他需求的已归档 FP（历史基线，可被新需求引用）
        跨需求名称存在多个同名候选（不同需求同名 FP）时判定为歧义，
        跳过该边并记录告警，避免建错依赖（宁缺毋滥，可人工补边）。
        跨需求边仍以 pending 状态创建，在本需求确认/归档时随本需求 FP
        自动审核为 approved 而生效（与既有归档自动审核机制衔接）。
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

    # v3.1.3：加载同产品下其他需求的已归档 FP（跨需求候选）。
    # 双索引：
    #   cross_name_to_id —— 精确名称 (name -> {req_id: fp_id})
    #   cross_norm_to_id —— 归一化名称（剥离 [v1.0]/（v1.0）后缀）后索引
    # LLM 引用历史功能点常带版本后缀（"权限控制（v1.0）"），归档 FP 名称是
    # 纯名称（"权限控制"），仅精确匹配必然落空，故构建归一化索引兜底。
    cross_name_to_id: dict[str, dict[str, str]] = {}
    cross_norm_to_id: dict[str, dict[str, str]] = {}
    try:
        req_obj = await session.get(Requirement, req_id)
        cross_result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.requirement_id)
            .join(Requirement, Requirement.id == FunctionalPoint.requirement_id)
            .where(
                Requirement.product_id == req_obj.product_id,
                FunctionalPoint.requirement_id != req_id,
                FunctionalPoint.status == "archived",
            )
        )
        for fid, fname, freq_id in cross_result.all():
            if fname:
                cross_name_to_id.setdefault(fname, {})[freq_id] = fid
                norm = _normalize_fp_ref(fname)
                if norm:
                    cross_norm_to_id.setdefault(norm, {})[freq_id] = fid
    except Exception as e:
        logger.warning("[FP-Dep] 跨需求 FP 候选加载失败（降级仅本需求解析）: {}", str(e)[:150])
        cross_name_to_id = {}
        cross_norm_to_id = {}

    def _resolve_fp_id(name: str) -> str | None:
        """名称解析（按优先级）：
        1. 本需求精确名
        2. 本需求归一化名（去版本后缀，兼容 LLM 写法）
        3. 跨需求已归档精确名（候选唯一时）
        4. 跨需求已归档归一化名（候选唯一时）
        歧义（多个同名候选）时跳过该边并告警，避免建错依赖（宁缺毋滥，可人工补边）。
        """
        if not name:
            return None
        fid = name_to_id.get(name)
        if fid:
            return fid
        norm = _normalize_fp_ref(name)
        if norm and norm != name:
            fid = name_to_id.get(norm)
            if fid:
                return fid
        for candidates in (cross_name_to_id.get(name) or {}, cross_norm_to_id.get(norm) or {}):
            if len(candidates) == 1:
                return next(iter(candidates.values()))
            if len(candidates) > 1:
                logger.warning(
                    "[FP-Dep] 跨需求功能点名存在歧义（{} 个同名候选），跳过该边: name={}",
                    len(candidates), name,
                )
        return None

    saved = 0
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        src_name = dep.get("source_fp_name", "")
        tgt_name = dep.get("target_fp_name", "")
        src_id = _resolve_fp_id(src_name)
        tgt_id = _resolve_fp_id(tgt_name)
        if not src_id or not tgt_id or src_id == tgt_id:
            continue
        relation = dep.get("relation_type", "depends_on")
        if relation not in ("depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"):
            relation = "depends_on"
        # 查重（含跨需求方向）
        # v3.5.7：scalar_one_or_none() → first()——存量脏数据存在多行时前者抛
        # MultipleResultsFound 直接炸掉保存本身；first() 只判「存在」。
        dup = await session.execute(
            select(FPDependency).where(
                FPDependency.source_fp_id == src_id,
                FPDependency.target_fp_id == tgt_id,
                FPDependency.relation_type == relation,
            )
        )
        if dup.first():
            continue
        # v3.5.7（P1-B 对偶边治理）：depends_on 建边前检查反向矛盾边——
        # 「A depends_on B」与「B depends_on A」并存即逻辑矛盾（图谱成环、
        # 影响分析 BFS 范围虚增）。发现反向边已存在时跳过本边并告警（宁缺毋滥）。
        if relation == "depends_on":
            rev = await session.execute(
                select(FPDependency).where(
                    FPDependency.source_fp_id == tgt_id,
                    FPDependency.target_fp_id == src_id,
                    FPDependency.relation_type == "depends_on",
                )
            )
            if rev.first():
                logger.warning(
                    "[FP-Dep] 检测到反向 depends_on 边已存在，跳过本边（防对偶成环）: "
                    "src_fp={} tgt_fp={}（已有 {}→{}）",
                    src_id, tgt_id, tgt_id, src_id,
                )
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


_DOCX_MAGIC = b"PK\x03\x04"  # Office Open XML 文件头（.docx）
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

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

            stmt = sa_update(AnalysisTask).where(
                AnalysisTask.id == task_id,
                AnalysisTask.status != "interrupted",  # v2.8.52: 已取消的任务不再覆盖
            ).values(**fields)
            await s.execute(stmt)
            await s.commit()
    except Exception as e:
        # v3.4.1 修复 P-8：状态回写失败此前只记 warning，任务会长期停在旧状态
        # （如 running），进而被看门狗误判为僵尸。这里升级为 error 并带上目标状态，
        # 便于从日志定位"状态失真"根因；仍不抛出，避免打断主流水线。
        logger.error(
            "[Task] 更新任务状态失败（状态可能失真）: task_id={}, target_status={}, error={}",
            task_id, fields.get("status", "-"), str(e)[:200],
        )

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
    # P3-6: 后台协程设置任务环境上下文，LLM 日志自动关联 task/req
    from app.core.task_context import set_task_context
    set_task_context(task_id=task_id, req_id=req_id)

    async def _is_cancelled() -> bool:
        """检测任务是否被用户取消（status=interrupted）。"""
        try:
            async with async_session() as s:
                t = await s.get(AnalysisTask, task_id)
                return t is not None and t.status == "interrupted"
        except Exception:
            return False

    # docs/17：HTML 原型解析降级标记（phase 0 收集，随 SRR 落库透出）
    proto_flags_all: list[str] = []
    # v3.4.6（缺陷 B）：FP 提取降级标记。必须在 try 外初始化——
    # 否则任务整体失败走 except 分支时引用未定义变量会抛 NameError，
    # 把「LLM 不可用」变成「代码崩溃」（失败路径覆盖的典型坑）。
    fp_extract_degraded: bool = False
    fp_extract_error: str = ""

    try:
        # ===== phase 0: upload（文件解析）=====
        # v2.24: 文件解析从 API 请求移到协程中，任务记录可提前创建
        if file_bytes_list:
            await _update_task(task_id, status="running", phase="upload", progress=3,
                               message=f"正在解析 {len(file_bytes_list)} 个文件...")
            all_raw_images: list[dict] = []
            merged_parts: list[str] = []
            proto_stats_all: list[dict] = []
            for filename, raw_bytes in file_bytes_list:
                ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
                if ext in ("md", "markdown", "txt"):
                    text = raw_bytes.decode("utf-8", errors="replace")
                elif ext == "docx":
                    try:
                        async with async_session() as vis_session:
                            text, doc_images = await _extract_docx_text(raw_bytes, filename=filename, session=vis_session)
                        all_raw_images.extend(doc_images)
                    except Exception as e:
                        raise RuntimeError(f"解析 {filename} 失败: {str(e)[:200]}")
                elif ext in ("html", "htm"):
                    # docs/17：HTML 原型 → 语义树（与 _parse_uploaded_files 共用
                    # build_html_segment，标记与文本组装收敛单一实现，对称性由构造保证）
                    from app.utils.html_proto_extract import build_html_segment
                    try:
                        async with async_session() as vis_session:
                            segment, proto_images, proto_stats, proto_flags = await build_html_segment(
                                raw_bytes, filename=filename, session=vis_session,
                            )
                        all_raw_images.extend(proto_images)
                        proto_flags_all.extend(proto_flags)
                        proto_stats_all.append({"file": filename, **proto_stats})
                    except ImportError:
                        raise RuntimeError("beautifulsoup4 未安装，无法解析 .html 原型文件")
                    except Exception as e:
                        raise RuntimeError(f"解析 {filename} 失败: {str(e)[:200]}")
                    merged_parts.append(segment)
                    continue
                else:
                    text = raw_bytes.decode("utf-8", errors="replace")
                # 非 HTML 来源：沿用【文档：】边界标记（HTML 分支已 append+continue）
                merged_parts.append(f"【文档：{filename}】\n{text}")
            combined_raw = "\n\n---\n\n".join(merged_parts)

            # docs/17：原型解析降级标记与统计落库（前端任务详情可展示）
            if proto_flags_all or proto_stats_all:
                combined_raw += (
                    "\n\n# 原型解析统计：" + "; ".join(
                        f"{s['file']}: {s.get('fields', 0)} 个字段、{s.get('buttons', 0)} 个交互元素、"
                        f"{s.get('tables', 0)} 个表格、{s.get('images', 0)} 张图片"
                        for s in proto_stats_all
                    )
                )

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

        parse_result = await req_agent.parse(
            raw_requirement=combined_raw,
            product_id=product_id,
            product_name=product_name or "",
        )
        srr = parse_result.srr
        if not isinstance(srr, dict):
            raise RuntimeError(f"需求解析返回异常类型: {type(srr).__name__}")

        # v2.8.52: 从 ParseResult 元组捕获 FP（不再依赖实例属性）
        captured_fps = parse_result.fps
        captured_reused_fps = getattr(parse_result, "reused_fps", []) or []
        captured_fp_deps = parse_result.fp_deps
        captured_interaction_srr = parse_result.interaction_srr
        # v3.4.6（缺陷 B）：在调用点立即捕获 FP 提取降级标记。
        # 不能等到出口读 srr_json_data——中间可能被 ClarifyAgent 的 augmented_srr
        # （新字典）替换，标记会丢失。此处捕获后随 result_json 透出。
        fp_extract_degraded, fp_extract_error = _read_fp_extract_degrade(srr)
        if fp_extract_degraded:
            logger.warning(
                "[Task] ⚠️ FP 提取降级（LLM 不可用/返回异常），本次 0 功能点: req_id={}, err={}",
                req_id, fp_extract_error[:200],
            )

        if await _is_cancelled():
            logger.info("[Task] 任务已取消，提前退出: task_id={}", task_id)
            return

        req_data = srr.setdefault("requirement", {})
        # 对齐 SRR 内部 ID 为 DB req_id（避免 ClarifyAgent PG 增强查不到记录）
        req_data["id"] = req_id
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

        # ===== 阶段 2：知识库检索（kb_search）+ 澄清（clarify） =====
        await _update_task(task_id, phase="kb_search", progress=40,
                           message="正在检索知识库历史需求并补充缺失字段...")

        # v2.8.57: 澄清进度回调——将 ClarifyAgent 内部 LLM 调用级进度上报到任务
        async def _clarify_progress_cb(message: str, progress: int) -> None:
            await _update_task(task_id, phase="kb_search", progress=progress, message=message)

        clarify_result = None
        questions = []
        clarify_error: str | None = None
        async with async_session() as s:
            try:
                clarify_result = await clarify_agent.clarify(
                    srr, progress_callback=_clarify_progress_cb,
                )
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
                    await _save_fps_to_db(save_s, req_id, captured_fps, req_data.get("priority", "P2"), product_id=product_id)
                    # v3.5.7（P1-A）：复用副本登记（原实现被过滤丢弃，全复用场景 FP 恒 0）
                    await _register_reused_fps(save_s, req_id, captured_reused_fps, req_data.get("priority", "P2"))
                    await _save_fp_dependencies(save_s, req_id, captured_fp_deps)
                    await _update_fp_index_in_srr(save_s, req_id)
                    await save_s.commit()
                except ValueError as gate_err:
                    # v2.8.56: FP 质量门禁失败（ValueError）不降级保存，向上抛出中断流水线
                    await save_s.rollback()
                    logger.error("[Task] FP 拆分质量门禁未通过（不降级保存）: {}", str(gate_err)[:200])
                    raise
                except Exception as fp_e:
                    # v2.8.52: FP 保存失败时回滚整个事务，避免半写入状态
                    await save_s.rollback()
                    logger.error("[Task] ClarifyAgent 失败后基础 FP 保存也失败，事务已回滚: {}", str(fp_e)[:200])
                    # 至少保存 Requirement（不带 FP），让用户能看到 SRR
                    async with async_session() as retry_s:
                        await retry_s.execute(sa_update(ReqModel).where(ReqModel.id == req_id).values(
                            status="draft",
                            srr_json=base_srr,
                        ))
                        await retry_s.commit()

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
                # v3.3（N7）：语义检测层降级标记落库——否则"质量分很高但语义层
                # 什么都没检测"这一事实只存在于日志里，前端与后续流程都看不到。
                "degraded_flags": clarify_result.get("degraded_flags", []),
                "semantic_degraded": clarify_result.get("semantic_degraded", False),
            }
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                srr_json_data["_kb_query_logs"] = kb_logs

        needs_clarify = (clarify_result.get("status") == "needs_clarification") if clarify_result else (len(questions) > 0)
        # v1.6：分析完成后进入 pending_review（待确认），等待用户确认 SRR/FP/影响分析
        final_status = "draft" if needs_clarify else "pending_review"

        # ===== 甲1：轻量质量门禁（温和提示，不强制阻断）=====
        # ClarifyAgent 已算出 quality_report.overall_score，此处消费它：
        # - score < QUALITY_WARN_THRESHOLD（0.4）时，即使 needs_clarify=False 也标记质量警告
        # - 不改 status 流转（避免过度阻断），仅在 result_json 中携带 quality_warning 供前端提示
        QUALITY_WARN_THRESHOLD = 0.4
        quality_report = (clarify_result or {}).get("quality_report", {}) or {}
        overall_score = quality_report.get("overall_score")
        quality_warning: str | None = None
        if overall_score is not None and overall_score < QUALITY_WARN_THRESHOLD:
            quality_warning = (
                f"需求分析质量评分偏低（{overall_score:.2f} < {QUALITY_WARN_THRESHOLD}），"
                "建议先补充澄清或重新生成再确认，避免后续用例生成返工。"
            )
            logger.warning("[Task] 需求质量门禁警告: req_id={}, score={:.2f}", req_id, overall_score)

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
                regen_result = await req_agent.parse(
                    raw_requirement=enhanced_raw,
                    product_id=product_id or "PROD-DEFAULT",
                )
                final_srr = regen_result.srr
                final_srr.setdefault("requirement", {})["id"] = req_id
                # v2.8.52: regen 成功后更新 FP 为第二次 parse 的结果（保证 SRR/FP 一致）
                captured_fps = regen_result.fps
                captured_fp_deps = regen_result.fp_deps
                captured_interaction_srr = regen_result.interaction_srr
                final_srr_data = dict(final_srr)
                # 保留 KB 日志
                if clarify_result:
                    kb_logs = clarify_result.get("kb_access_log", [])
                    if kb_logs:
                        final_srr_data["_kb_query_logs"] = kb_logs
                final_srr_data.pop("_clarify_state", None)
                final_srr_data.pop("_field_sources", None)
                srr_json_data = final_srr_data
                # v3.4.6（缺陷 B）：本段是第二次 parse，FP 标记在新 SRR 上——
                # 若本次才失败，需覆盖首次的结果（以最新一次为准）。
                if regen_result.srr.get("_fp_extract_degraded"):
                    fp_extract_degraded = True
                    fp_extract_error = str(regen_result.srr.get("_fp_extract_error", "") or "")
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

        # ===== 持久化 Requirement + FP（合并为单事务，v2.8.52 修复事务拆分）=====
        # v2.18: 合并 interaction_srr 到总 SRR（统一所有流程的 interaction_srr 处理）
        if captured_interaction_srr:
            srr_json_data = _merge_interaction_srr(srr_json_data, captured_interaction_srr)

        # docs/17：原型解析降级标记随 SRR 落库透出（前端可见，不静默）
        if proto_flags_all:
            srr_json_data["_proto_degraded_flags"] = proto_flags_all

        # v3.4.6（缺陷 B 补漏）：FP 提取降级标记**随 SRR 落库**（下称"落库标记"）。
        # 为什么必须落库、不能只透出 result_json：
        #   ① 生成阶段（流水线 `_parse_node`）读的是 `Requirement.srr_json`，
        #      **不读**分析任务的 `result_json` —— 只透出后者时，生成侧降级门禁
        #      完全无从判定（这正是 2026-09-28 审计发现的断裂点）；
        #   ② `srr_json` 是需求的权威内容（会被导出、被下游消费），而降级事实
        #      目前只存在于一次性的任务 result_json 里，离开详情页即不可见。
        # ⚠️ 为什么必须在此处**重新补写**（而非依赖上面已有的标记）：
        #   第 1277 行 `srr_json_data = dict(multi_augmented)` —— 当 ClarifyAgent
        #   返回 `augmented_srr`（一个**新字典**）时，ReqAgent 打在原始 `srr` 上的
        #   `_fp_extract_degraded` **不会**进入落库字典。调用点捕获（result_json
        #   透出）与落库标记是**两个独立的到达性问题**，必须分别保证。
        if fp_extract_degraded:
            srr_json_data["_fp_extract_degraded"] = True
            srr_json_data["_fp_extract_error"] = fp_extract_error

        # v2.18: FP 保存失败不再静默——中断流水线，标记任务失败（FP 是核心产物，0 FP 会导致后续流程全部失效）
        if await _is_cancelled():
            logger.info("[Task] 任务已取消（持久化前），提前退出: task_id={}", task_id)
            return

        try:
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
                # v1.5.1: 自动保存 ReqAgent 一体式提取的功能点 + 依赖关系
                await _save_fps_to_db(s, req_id, captured_fps, req_data.get("priority", "P2"), product_id=product_id)
                # v3.5.7（P1-A）：复用副本登记
                await _register_reused_fps(s, req_id, captured_reused_fps, req_data.get("priority", "P2"))
                await _save_fp_dependencies(s, req_id, captured_fp_deps)
                await _update_fp_index_in_srr(s, req_id)  # v2.18: 更新总 SRR 的 fp_index
                await s.commit()
        except Exception as e:
            fp_err_msg = str(e)[:500]
            logger.error("[Task] Requirement/FP 保存失败（关键错误，中断流水线）: {}", fp_err_msg)
            # v2.8.53: 回滚 Requirement status 到 draft，避免卡死在 analyzing 状态
            # （事务已回滚，status 保持 analyzing，前端不允许操作 analyzing 状态的需求）
            try:
                async with async_session() as rollback_s:
                    await rollback_s.execute(sa_update(ReqModel).where(ReqModel.id == req_id).values(status="draft"))
                    await rollback_s.commit()
            except Exception as revert_err:
                logger.warning("[Task] 状态回滚失败: req_id={}, error={}", req_id, str(revert_err)[:150])
            err_msg = f"保存失败: {fp_err_msg}"
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
        # v3.5.7（P1-A）：复用副本透出（用户可见「哪些 FP 是复用基线」）
        if captured_reused_fps:
            result["reused_fps"] = [
                {"name": r.get("name", ""), "source_fp_id": r.get("source_fp_id", "")}
                for r in captured_reused_fps if isinstance(r, dict)
            ]
        # v2.18: 降级警告传递给前端
        if srr_json_data.get("_finalize_degraded"):
            result["degraded"] = True
            result["degraded_reason"] = srr_json_data.get("_finalize_error", "最终 SRR 生成失败，已降级使用首次解析结果")
        # docs/17: 原型解析降级透出（对齐既有 result_json.degraded 契约，前端任务详情可见）
        # 失败路径不静默：4 类标记（截断/超时/解析失败/无语义）置 degraded + 可见文案；
        # 同时保留结构化的 proto_degraded_flags 供前端逐条渲染与后续自动化消费。
        if proto_flags_all:
            result["proto_degraded_flags"] = proto_flags_all
            if not result.get("degraded"):
                result["degraded"] = True
                result["degraded_reason"] = (
                    "HTML 原型解析降级（" + "、".join(proto_flags_all)
                    + "），原型语义可能不完整，请人工核对相关要点"
                )
        # v3.4.6（缺陷 B）：FP 提取失败降级透出。
        # 反例（2026-09-28 实测）：LLM 首响应超时 → FP 提取返回 0 个 →
        # result_json 里只有「0 个功能点」这个结果值，**没有任何失败证据**，
        # 任务仍 completed。用户无法区分「需求确实没有功能点」与「LLM 挂了」。
        # 修复：把 _fp_extract_from_raw 打在 SRR 上的标记透出为结构化字段，
        # 并置 degraded（0 FP 会让 FP 分片通道全部退化，必须可见）。
        # 统一走 _apply_fp_extract_degrade（同一函数也用于两条失败路径，
        # 避免 6 处相似代码里漏改 except——参见该函数 docstring）。
        _apply_fp_extract_degrade(result, fp_extract_degraded, fp_extract_error)
        # 甲1: 质量门禁警告传递给前端
        if quality_warning:
            result["quality_warning"] = quality_warning
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
        # docs/17: 失败路径同样透出原型降级标记。
        # 反例（2026-09-28 实测）：纯壳原型 → 解析出 proto_no_semantics → LLM 报
        # "requirement.title 缺失" → 任务 failed，但失败分支原先不写 result_json，
        # 用户只看到与根因无关的 "title 缺失"，真正的根因标记被丢掉。
        # 纪律：fail-closed 要落成**终态**而非异常，标记必须到达消费方。
        failed_result: dict = {}
        if proto_flags_all:
            failed_result["proto_degraded_flags"] = proto_flags_all
            failed_result["degraded"] = True
            failed_result["degraded_reason"] = (
                "HTML 原型解析降级（" + "、".join(proto_flags_all)
                + "），原型语义可能不完整，请人工核对相关要点"
            )
        # v3.4.6（缺陷 B）：失败路径同样透出 FP 提取降级标记。
        # 纪律：打标记类补丁只覆盖 happy path 是头号 bug——任务 failed 时若
        # 根因包含「FP 提取失败」，必须在 result_json 里留下证据，否则用户
        # 只看到与根因无关的报错，等于标记从未到达消费方。
        # 失败路径覆盖：降级原因优先保留既有 degraded_reason，FP 证据独立成字段。
        _apply_fp_extract_degrade(
            failed_result, fp_extract_degraded, fp_extract_error,
            reason=_FP_DEGRADE_REASON_FAILED,
        )
        await _update_task(
            task_id, status="failed", phase="", error=err_msg,
            message="分析失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
            result_json=failed_result or None,
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

async def _run_clarify_reparse(
    task_id: str,
    req_id: str,
    enhanced_raw: str,
    original_raw: str,  # v1.6: 原始需求文本（用于 FP 提取，避免增强文本导致 FP 偏差）
    product_id: str,
    clarify_history: list,
    old_srr: dict,
    priority: str,
    original_status: str = "draft",  # v2.8.52: 原始状态快照，失败时恢复
) -> None:
    """后台执行澄清后的 SRR 重新生成（异步协程，独立 session）。

    流程：
        1. phase=parse: req_agent.parse() 重新生成 SRR（含降级处理）
        2. 持久化 SRR + 保留 KB 日志 + 迁移 _baseline_context
        3. FP 重提取（使用 original_raw 而非 enhanced_raw，确保与需求分析时一致）
        4. status=completed
    """
    from sqlalchemy import update as sa_update, delete as sa_del
    # P3-6: 后台协程设置任务环境上下文，LLM 日志自动关联 task/req
    from app.core.task_context import set_task_context
    set_task_context(task_id=task_id, req_id=req_id)
    # v3.4.6（缺陷 B）：必须在 try 外初始化——except 分支要引用，否则 NameError。
    fp_extract_degraded: bool = False
    fp_extract_error: str = ""
    try:
        await _update_task(task_id, status="running", phase="parse", progress=20,
                           message="正在调用 LLM 重新生成 SRR...")

        # ===== 重新调用 ReqAgent 生成最终 SRR =====
        try:
            clarify_result = await req_agent.parse(
                raw_requirement=enhanced_raw,
                product_id=product_id,
            )
            final_srr = clarify_result.srr
            final_srr.setdefault("requirement", {})["id"] = req_id
            logger.info("[Task/Clarify] 最终 SRR 生成成功: req_id={}", req_id)
        except Exception as e:
            logger.error("[Task/Clarify] 最终 SRR 生成失败 → 降级: {}", str(e)[:200])
            # 降级：使用原有 SRR
            final_srr = dict(old_srr)
            final_srr.pop("_clarify_state", None)
            final_srr.pop("clarify_answers", None)
            final_srr.pop("_field_sources", None)
            final_srr["_clarify_degraded"] = True

        # v2.8.52: 从 ParseResult 元组捕获 FP（不再依赖实例属性）
        # v1.6: 使用 original_raw（原始需求文本）进行 FP 提取，而非 enhanced_raw。
        # v3.4.6（缺陷 B）：显式传 srr=final_srr，让 _fp_extract_from_raw 能把
        # 降级标记打在本次任务的 SRR 上（原先漏传，失败无从察觉）。
        if original_raw:
            try:
                captured_fps, captured_reused_fps, captured_fp_deps, captured_interaction_srr = await req_agent._fp_extract_from_raw(
                    raw_text=original_raw,
                    srr=final_srr,
                )
                logger.info(
                    "[Task/Clarify] FP 重提取完成（基于原始文本）: {} 个新, {} 个复用",
                    len(captured_fps), len(captured_reused_fps),
                )
                fp_extract_degraded, fp_extract_error = _read_fp_extract_degrade(final_srr)
            except Exception as fp_e:
                logger.warning("[Task/Clarify] FP 重提取失败（降级使用 enhanced_raw 结果）: {}", str(fp_e)[:200])
                captured_fps = clarify_result.fps
                captured_reused_fps = getattr(clarify_result, "reused_fps", []) or []
                captured_fp_deps = clarify_result.fp_deps
                captured_interaction_srr = clarify_result.interaction_srr
                fp_extract_degraded = True
                fp_extract_error = f"{type(fp_e).__name__}: {str(fp_e)[:200]}"
        else:
            captured_fps = clarify_result.fps
            captured_reused_fps = getattr(clarify_result, "reused_fps", []) or []
            captured_fp_deps = clarify_result.fp_deps
            captured_interaction_srr = clarify_result.interaction_srr
        if fp_extract_degraded:
            logger.warning(
                "[Task/Clarify] ⚠️ FP 提取降级（本次 0 功能点，非「需求无功能点」）: req_id={}, err={}",
                req_id, fp_extract_error[:200],
            )

        await _update_task(task_id, phase="parse", progress=60,
                           message="SRR 生成完成，正在保存功能点...")

        # ===== 持久化 =====
        final_data = dict(final_srr)
        final_data.pop("_clarify_state", None)
        final_data.pop("clarify_answers", None)
        final_data.pop("_field_sources", None)

        # v3.0.1: 澄清重解析后，将 _raw_text 恢复为原始需求文本（original_raw），
        # 而非增强文本（enhanced_raw）。修复根因：重解析时 req_agent.parse(enhanced_raw)
        # 会把含「补充信息（用户澄清）」的回填文本写入 _raw_text，导致下一轮 ClarifyAgent
        # 基于 _raw_text 检测引用未定义时，把用户澄清问题里的「引用未定义」关键词当作
        # 新的引用未定义，造成同一主题反复提问、澄清无法收敛。
        if original_raw and original_raw.strip():
            final_data["_raw_text"] = original_raw

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
                    # v2.8.53: 用户已选"不影响历史数据，仅新增功能" -> is_optimization 设为 False
                    # 避免后续 ClarifyAgent 二次验证时再次生成历史数据问题（死循环提问）
                    no_impact_selected = (
                        "不影响历史数据" in impact_types
                        or "仅新增功能" in impact_types
                    )
                    if no_impact_selected:
                        ldi["is_optimization"] = False
                        logger.info(
                            "[Task/Clarify] 用户选择不影响历史数据，is_optimization 设为 False: req_id={}",
                            req_id,
                        )
                    else:
                        # 若 LLM 未标记 is_optimization，用户回答了影响问题则标记为 True
                        ldi.setdefault("is_optimization", True)
                    final_data["legacy_data_impact"] = ldi
                    logger.info(
                        "[Task/Clarify] 写入 legacy_data_impact: req_id={}, fields={}",
                        req_id, updated_fields,
                    )
        except Exception as ldi_e:
            logger.warning("[Task/Clarify] 写入 legacy_data_impact 失败(不阻断): {}", str(ldi_e)[:200])

        # ===== 甲2+丙：重解析后再验证（答案覆盖评估）+ 轻量质量门禁 =====
        # v3.0.1 多轮收敛重构：不再从零做完整缺口检测生成整轮新问题，
        # 而是评估「上一轮问题的答案是否覆盖」。覆盖 → 收敛进入 pending_review；
        # 未覆盖/模糊 → 仅对这些问题追问；用户跳过 → 视为不关注，不再追问。
        # 正常 1 轮正确回答即完全收敛，不会出现第二轮。
        recheck_questions: list = []
        recheck_quality_warning: str | None = None
        try:
            await _update_task(task_id, phase="recheck", progress=88,
                               message="正在评估上一轮澄清答案的覆盖情况...")
            async with async_session() as s:
                recheck_result = await clarify_agent.eval_answer_coverage(
                    final_data, clarify_history=clarify_history,
                )
            recheck_questions = recheck_result.get("questions", []) or []
            recheck_quality_report = dict(recheck_result.get("quality_report", {}) or {})
            # v3.3 口径分离（docs/14 一期第 4 项）：
            # recheck 的 overall_score 是**答案覆盖率**，与需求级**质量分**
            # （clarify() 的 _calc_quality_score）不是同一个量。此前直接整体覆盖，
            # 导致落库/前端展示的质量分被覆盖率顶掉（旧实现更是硬编码 1.0/0.8）。
            # 现改为合并：质量分保留，覆盖率单独记到 coverage_score。
            recheck_coverage_score = recheck_quality_report.pop("overall_score", None)
            # 覆盖率自己的说明单独存，避免顶掉质量分的 summary
            _coverage_summary = recheck_quality_report.pop("summary", None)
            recheck_score = recheck_coverage_score  # 下方警告阈值仍按覆盖率口径判断
            _prev_qr = (final_data.get("_clarify_state") or {}).get("quality_report") or {}
            _merged_qr = {**(_prev_qr if isinstance(_prev_qr, dict) else {}), **recheck_quality_report}
            if _coverage_summary:
                _merged_qr["coverage_summary"] = _coverage_summary
            if recheck_coverage_score is not None:
                _merged_qr["coverage_score"] = recheck_coverage_score
            elif "coverage_score" not in _merged_qr:
                # 无覆盖证据时显式标注，避免前端把缺失当成 0 分
                _merged_qr["coverage_score"] = None
            if "overall_score" not in _merged_qr and recheck_coverage_score is not None:
                # 兜底：理论上不应发生（首次分析必写入质量分），避免前端显示 0
                _merged_qr["overall_score"] = recheck_coverage_score
            # 写入最新的 _clarify_state（问题/覆盖率取本次，质量分沿用需求级）
            final_data["_clarify_state"] = {
                "questions": recheck_questions,
                "auto_augmentations": recheck_result.get("auto_augmentations", []),
                "kb_status": recheck_result.get("kb_status", {}),
                "quality_report": _merged_qr,
                "field_conflicts": recheck_result.get("field_conflicts", []),
                # v3.3（N7）：同 1160 处，降级标记随本次重解析结果一并落库
                "degraded_flags": recheck_result.get("degraded_flags", []),
                "semantic_degraded": recheck_result.get("semantic_degraded", False),
            }
            recheck_kb_logs = recheck_result.get("kb_access_log", [])
            if recheck_kb_logs:
                final_data["_kb_query_logs"] = recheck_kb_logs

            # 甲2: 质量门禁（温和提示）
            QUALITY_WARN_THRESHOLD = 0.4
            if recheck_score is not None and recheck_score < QUALITY_WARN_THRESHOLD:
                recheck_quality_warning = (
                    f"重解析后质量评分偏低（{recheck_score:.2f} < {QUALITY_WARN_THRESHOLD}），"
                    "建议补充澄清或重新生成再确认。"
                )
                logger.warning("[Task/Clarify] 重解析质量门禁警告: req_id={}, score={:.2f}", req_id, recheck_score)
            logger.info(
                "[Task/Clarify] 重解析再验证完成: req_id={}, new_questions={}, score={}",
                req_id, len(recheck_questions), recheck_score,
            )
        except Exception as recheck_e:
            # v2.41: 再验证失败不再静默——标记 _recheck_skipped，confirm 时会拦截提示用户人工复核
            logger.warning("[Task/Clarify] 重解析再验证失败(已标记_recheck_skipped): {}", str(recheck_e)[:200])
            final_data["_recheck_skipped"] = True
            final_data["_recheck_error"] = str(recheck_e)[:300]

        # 丙: 若再验证发现新澄清问题，status 改为 draft 让用户先补充；否则 pending_review
        # v2.42: 3 轮收敛（docs/01 §4.1 第4条 + §4.2 流程图）
        # 第 3 轮仍有未解决问题 → 强制放行，标记 _clarify_force_passed + 未解决问题降级为 optional
        # v2.43: 轮次统计基于"当前重新生成周期"，重新生成后重置计数
        # v2.8.51: 从 settings 读取，与 orchestrator ClarifyLoopGuard 保持一致
        MAX_CLARIFY_ITERATIONS = settings.MAX_CLARIFY_ITERATIONS
        clarify_rounds_done = _count_clarify_rounds_since_last_regen(clarify_history)
        force_passed = (
            recheck_questions
            and clarify_rounds_done >= MAX_CLARIFY_ITERATIONS
        )
        if force_passed:
            # 将未解决问题降级为 optional，写入 _clarify_state 供前端展示但不再阻断
            for q in recheck_questions:
                if isinstance(q, dict):
                    q["priority"] = "optional"
                    q["_force_downgraded"] = True
            final_data["_clarify_force_passed"] = True
            final_data["_clarify_force_passed_reason"] = (
                f"已达到最大澄清轮次（{MAX_CLARIFY_ITERATIONS} 轮），剩余 "
                f"{len(recheck_questions)} 个问题降级为 optional，不阻断流程。"
                "建议在确认前人工复核这些问题。"
            )
            recheck_final_status = "pending_review"
            recheck_done_msg = (
                f"已达到最大澄清轮次（{MAX_CLARIFY_ITERATIONS} 轮），"
                f"剩余 {len(recheck_questions)} 个问题降级为 optional，需求进入待确认"
            )
            logger.warning(
                "[Task/Clarify] 3轮收敛强制放行: req_id={}, remaining_questions={}",
                req_id, len(recheck_questions),
            )
        elif recheck_questions:
            recheck_final_status = "draft"
            recheck_done_msg = "重解析完成，发现新的待澄清问题，需求进入草稿状态"
        else:
            recheck_final_status = "pending_review"
            recheck_done_msg = "澄清完成，需求进入待确认"

        async with async_session() as s:
            await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(
                srr_json=final_data, status=recheck_final_status,
                # v2.33: 自增 revision（澄清重解析属于内容变更）
                revision=Requirement.revision + 1,
                # v2.8.52: 同步更新标题（LLM 重解析可能产生新标题）
                title=final_data.get("requirement", {}).get("title", ""),
            ))
            await s.commit()

        logger.info(
            "[Task/Clarify] 澄清重解析完成，需求进入待确认: req_id={}, title={}",
            req_id,
            final_data.get("requirement", {}).get("title", ""),
        )

        # ===== FP 重提取（含 P3 修复：清理旧 FP 向量）=====
        # v3.5.7（P1-A）：`if captured_fps` → `if captured_fps or captured_reused_fps`，
        # 全复用场景（新 FP 为 0 但有合法复用副本）不再跳过整个 FP 更新块。
        if captured_fps or captured_reused_fps:
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

                await _save_fps_to_db(s, req_id, captured_fps, priority, product_id=product_id)
                # v3.5.7（P1-A）：复用副本登记
                await _register_reused_fps(s, req_id, captured_reused_fps, priority)
                await _save_fp_dependencies(s, req_id, captured_fp_deps)
                await _update_fp_index_in_srr(s, req_id)  # v2.18: 更新总 SRR 的 fp_index
                await s.commit()
                logger.info(
                    "[Task/Clarify] 澄清后重新提取 FP: {} 个新, {} 个复用",
                    len(captured_fps), len(captured_reused_fps),
                )

        # 甲2+丙: result 携带再验证结果和质量警告
        recheck_result_json: dict = {"req_id": req_id, "status": recheck_final_status}
        if recheck_questions:
            recheck_result_json["new_questions"] = len(recheck_questions)
        # v3.5.7（P1-A）：复用副本透出
        if captured_reused_fps:
            recheck_result_json["reused_fps"] = [
                {"name": r.get("name", ""), "source_fp_id": r.get("source_fp_id", "")}
                for r in captured_reused_fps if isinstance(r, dict)
            ]
        if recheck_quality_warning:
            recheck_result_json["quality_warning"] = recheck_quality_warning
        # v3.4.6（缺陷 B）：FP 提取失败降级透出（与 _run_requirement_analysis 对称）。
        # 本入口在 original_raw 存在时重跑 _fp_extract_from_raw，同样可能失败静默。
        _apply_fp_extract_degrade(recheck_result_json, fp_extract_degraded, fp_extract_error)
        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message=recheck_done_msg,
            result_json=recheck_result_json,
            completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task/Clarify] 澄清重生成完成: task_id={}, req_id={}", task_id, req_id)

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task/Clarify] 澄清重生成失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        # v3.4.6（缺陷 B）：失败路径同样透出 FP 提取降级标记。
        # 纪律：打标记类补丁只覆盖 happy path 是头号 bug——本入口原先 except
        # 分支完全不写 result_json，任务 failed 时 FP 降级证据被整个丢掉。
        recheck_failed_result: dict = {}
        _apply_fp_extract_degrade(
            recheck_failed_result, fp_extract_degraded, fp_extract_error,
            reason=_FP_DEGRADE_REASON_FAILED,
        )
        await _update_task(
            task_id, status="failed", error=err_msg,
            message="澄清重生成失败: " + err_msg[:200],
            completed_at=datetime.now(timezone.utc),
            result_json=recheck_failed_result or None,
        )
        # 失败时将需求状态恢复为原始状态（v2.8.52: 不再无条件降级为 draft）
        try:
            async with async_session() as s:
                await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(status=original_status))
                await s.commit()
        except Exception as revert_err:
            logger.warning("[Task/Clarify] 失败回退状态失败: req_id={}, error={}", req_id, str(revert_err)[:150])

async def _run_regenerate_reparse(
    task_id: str,
    req_id: str,
    enhanced_raw: str,
    original_raw: str,
    product_id: str,
    previous_title: str,
    old_srr: dict,
    extra_notes: str,
    supplementary_text: str | None = None,
    original_status: str = "draft",
) -> None:
    """后台执行重新生成 SRR（异步协程，独立 session）。

    v2.42 变更：重新生成走完整需求分析流程，与 _run_requirement_analysis 一致。
    原始资料 = 原有解析SRR + 功能点SRR + 新上传文件 + 用户描述（已在端点合并为 enhanced_raw）。
    流程：
        1. phase=parse: req_agent.parse() 从合并文本重新解析生成新 SRR
        2. FP 提取（基于 original_raw，与需求分析一致）
        3. phase=kb_search: ClarifyAgent 检测（KB检索+问题生成+质量评分）
        4. phase=recheck: ClarifyAgent 二次验证（与 _run_clarify_reparse 一致）
        5. 3轮收敛检查（第3轮仍有问题→强制放行，标记optional）
        6. 持久化 + 版本快照 + FP 替换
        7. status=draft/pending_review

    v2.0 历史版本管理：
        重新生成前把原 srr_json 完整快照保存到 _version_history 数组，
        保留历次重新分析前的需求分析结果，供用户回溯。

    v2.17 支持补充文件上传：
        若 supplementary_text 非空，持久化时更新 supplementary_text 字段。
        原始 raw_text 保持不变，补充文件内容独立存储，前端可分别查看。
    """
    from sqlalchemy import update as sa_update, delete as sa_delete
    # v3.4.6（缺陷 B）：必须在 try 外初始化——except 分支要引用，否则 NameError。
    fp_extract_degraded: bool = False
    fp_extract_error: str = ""
    try:
        await _update_task(task_id, status="running", phase="parse", progress=20,
                           message="正在调用 LLM 重新解析需求...")

        # v2.42: 走完整需求分析流程——req_agent.parse（而非 refine_srr）
        try:
            regen_result = await req_agent.parse(
                raw_requirement=enhanced_raw,
                product_id=product_id,
            )
            final_srr = regen_result.srr
            final_srr.setdefault("requirement", {})["id"] = req_id
            logger.info("[Task/Regenerate] 重新解析 SRR 成功: req_id={}", req_id)
        except Exception as e:
            logger.error("[Task/Regenerate] 重新解析 SRR 失败 → 降级: {}", str(e)[:200])
            final_srr = dict(old_srr)
            final_srr.pop("_clarify_state", None)
            final_srr.pop("clarify_answers", None)
            final_srr.pop("_field_sources", None)
            final_srr["_clarify_degraded"] = True

        # v2.8.52: 从 ParseResult 元组捕获 FP（不再依赖实例属性）
        # v3.4.6（缺陷 B）：捕获 FP 提取降级标记（在调用点捕获，避免中间层覆盖）。
        captured_fps: list = []
        captured_reused_fps: list = []
        captured_fp_deps: list = []
        captured_interaction_srr = None
        if original_raw:
            try:
                captured_fps, captured_reused_fps, captured_fp_deps, captured_interaction_srr = await req_agent._fp_extract_from_raw(
                    raw_text=original_raw,
                    srr=final_srr,
                )
                logger.info(
                    "[Task/Regenerate] FP 提取完成（基于原始文本）: {} 个新, {} 个复用",
                    len(captured_fps), len(captured_reused_fps),
                )
                fp_extract_degraded, fp_extract_error = _read_fp_extract_degrade(final_srr)
                if fp_extract_degraded:
                    logger.warning(
                        "[Task/Regenerate] ⚠️ FP 提取降级（本次 0 功能点，非「需求无功能点」）: req_id={}, err={}",
                        req_id, fp_extract_error[:200],
                    )
            except Exception as fp_e:
                logger.warning("[Task/Regenerate] FP 提取失败（降级使用 parse 结果）: {}", str(fp_e)[:200])
                captured_fps = regen_result.fps
                captured_reused_fps = getattr(regen_result, "reused_fps", []) or []
                captured_fp_deps = regen_result.fp_deps
                captured_interaction_srr = regen_result.interaction_srr
        else:
            captured_fps = regen_result.fps
            captured_reused_fps = getattr(regen_result, "reused_fps", []) or []
            captured_interaction_srr = regen_result.interaction_srr

        await _update_task(task_id, phase="parse", progress=50, message="SRR 生成完成，正在检索知识库...")

        # ===== v2.32: phase=kb_search + clarify（与需求分析流程一致）=====
        # 重新生成同样需要走澄清环节：KB 检索 + 问题生成 + 质量评分
        # v2.8.57: 传入进度回调，将 ClarifyAgent 内部 40-70 区间映射到重新生成的 55-80
        await _update_task(task_id, phase="kb_search", progress=55,
                           message="正在检索知识库历史需求并补充缺失字段...")

        async def _regen_clarify_progress_cb(message: str, progress: int) -> None:
            # 线性映射：40-70 → 55-80
            mapped = 55 + round((progress - 40) * 25 / 30) if progress > 40 else 55
            await _update_task(task_id, phase="kb_search", progress=mapped, message=message)

        clarify_result = None
        questions: list = []
        clarify_error: str | None = None
        async with async_session() as s:
            try:
                clarify_result = await clarify_agent.clarify(
                    final_srr, progress_callback=_regen_clarify_progress_cb,
                )
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
                # v2.42: 使用 captured_fps（来自 _fp_extract_from_raw），与正常路径一致
                try:
                    if captured_fps or captured_reused_fps:
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
                        # 从 DB 查询 priority（clarify_error 分支中 record2 未定义）
                        req_row = (await save_s.execute(
                            select(Requirement.priority).where(Requirement.id == req_id)
                        )).scalar_one_or_none()
                        await _save_fps_to_db(save_s, req_id, captured_fps, req_row or "P2", product_id=product_id)
                        # v3.5.7（P1-A）：复用副本登记
                        await _register_reused_fps(save_s, req_id, captured_reused_fps, req_row or "P2")
                        await _save_fp_dependencies(save_s, req_id, captured_fp_deps)
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

        # ===== 甲3：轻量质量门禁（温和提示，与 _run_requirement_analysis 一致）=====
        QUALITY_WARN_THRESHOLD = 0.4
        regen_quality_report = (clarify_result or {}).get("quality_report", {}) or {}
        regen_overall_score = regen_quality_report.get("overall_score")
        regen_quality_warning: str | None = None
        if regen_overall_score is not None and regen_overall_score < QUALITY_WARN_THRESHOLD:
            regen_quality_warning = (
                f"重新生成后质量评分偏低（{regen_overall_score:.2f} < {QUALITY_WARN_THRESHOLD}），"
                "建议补充澄清或再次重新生成再确认。"
            )
            logger.warning("[Task/Regen] 重新生成质量门禁警告: req_id={}, score={:.2f}", req_id, regen_overall_score)

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
        # v2.8.52: 裁剪历史记录（最多保留最近 5 轮）
        if len(clarify_history) > 5:
            clarify_history = clarify_history[-5:]

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
        # v2.8.52: 裁剪历史版本，防止 srr_json 无界膨胀（最多保留最近 3 个版本）
        MAX_VERSION_HISTORY = 3
        if len(version_history) > MAX_VERSION_HISTORY:
            version_history = version_history[-MAX_VERSION_HISTORY:]
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
        # v2.8.53: 用户已答"不影响历史数据"（impact_scope 含标记）时也需保留，
        # 避免 ClarifyAgent 二次验证时因丢失用户答复而再次生成历史数据问题
        try:
            new_ldi = final_data.get("legacy_data_impact")
            old_ldi = old_srr.get("legacy_data_impact")
            old_has_user_answer = isinstance(old_ldi, dict) and (
                # 旧 ldi 标记为优化需求且有用户澄清文本
                (old_ldi.get("is_optimization") and old_ldi.get("user_clarification", "").strip())
                # v2.8.53: 或 impact_scope 含用户已答标记（如"不影响历史数据"）
                or any(m in str(old_ldi.get("impact_scope", "")) for m in ("不影响历史数据", "仅新增功能", "无影响"))
            )
            if old_has_user_answer:
                if isinstance(new_ldi, dict) and new_ldi.get("is_optimization"):
                    # LLM 重新生成了优化需求分析，回填旧的用户澄清（若新值为空）
                    if not str(new_ldi.get("user_clarification", "")).strip():
                        new_ldi["user_clarification"] = old_ldi.get("user_clarification", "")
                        # v2.8.53: 同步回填 impact_scope（含用户已答标记）
                        if not str(new_ldi.get("impact_scope", "")).strip():
                            new_ldi["impact_scope"] = old_ldi.get("impact_scope", "")
                        logger.info(
                            "[Task/Regenerate] 回填旧 user_clarification/impact_scope 到新 legacy_data_impact: req_id={}", req_id,
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

        # v2.42: 使用 captured_fps（来自 _fp_extract_from_raw 基于 original_raw），
        # 与 _run_requirement_analysis / _run_clarify_reparse 保持一致
        # v3.5.7（P1-A）：`if captured_fps` → `if captured_fps or captured_reused_fps`，
        # 全复用场景（新 FP 为 0 但有合法复用副本）不再跳过 FP 更新块。
        try:
            if captured_fps or captured_reused_fps:
                # 使用独立 session，不依赖 HTTP 请求的 session（此函数在后台协程运行）
                async with async_session() as fp_split_sess:
                    record2 = (await fp_split_sess.execute(
                        select(Requirement).where(Requirement.id == req_id)
                    )).scalar_one_or_none()
                    if record2:
                        logger.info(
                            "[Task/Regenerate] 应用提取的 FP: req_id={}, 新 FP={}, 复用={}",
                            req_id, len(captured_fps), len(captured_reused_fps),
                        )
                        from app.db.models import FunctionalPoint as FP, FPDependency
                        old_fp_rows = (await fp_split_sess.execute(
                            select(FP.id).where(FP.requirement_id == req_id)
                        )).scalars().all()
                        if old_fp_rows:
                            old_ids_set = set(old_fp_rows)
                            # P3 修复：清理旧 FP 向量
                            try:
                                from app.db.milvus_store import vector_store
                                for old_fp_id in old_ids_set:
                                    try:
                                        await asyncio.to_thread(
                                            vector_store.delete_requirement_from_kb, f"FP-{old_fp_id}"
                                        )
                                    except Exception as fp_vec_err:
                                        logger.warning(
                                            "[Task/Regenerate] 旧 FP 向量删除失败(不阻断): fp_id={}, error={}",
                                            old_fp_id, str(fp_vec_err)[:100],
                                        )
                            except Exception as vec_e:
                                logger.warning("[Task/Regenerate] FP 向量清理失败(不阻断): {}", str(vec_e)[:200])
                            await fp_split_sess.execute(
                                sa_delete(FPDependency).where(
                                    (FPDependency.source_fp_id.in_(old_ids_set)) |
                                    (FPDependency.target_fp_id.in_(old_ids_set))
                                )
                            )
                            await fp_split_sess.execute(sa_delete(FP).where(FP.requirement_id == req_id))
                            await fp_split_sess.flush()
                        await _save_fps_to_db(fp_split_sess, req_id, captured_fps, record2.priority, product_id=product_id)
                        # v3.5.7（P1-A）：复用副本登记
                        await _register_reused_fps(fp_split_sess, req_id, captured_reused_fps, record2.priority)
                        await _save_fp_dependencies(fp_split_sess, req_id, captured_fp_deps)
                        await _update_fp_index_in_srr(fp_split_sess, req_id)
                        await fp_split_sess.commit()
                        logger.info(
                            "[Task/Regenerate] FP 应用完成: req_id={}, 新 FP={}, 复用={}",
                            req_id, len(captured_fps), len(captured_reused_fps),
                        )
            else:
                logger.warning(
                    "[Task/Regenerate] FP 提取未产出结果（新 FP=0 且无合格复用副本），跳过 FP 更新: req_id={}",
                    req_id,
                )
        except Exception as fp_err:
            # v2.18: FP 应用失败不再静默——中断流水线，标记任务失败
            fp_err_msg = str(fp_err)[:500]
            logger.error("[Task/Regenerate] FP 应用失败（关键错误，中断流水线）: req_id={}, err={}", req_id, fp_err_msg)
            # v2.8.53: 回滚 Requirement status 到 original_status，避免状态与 FP 数据不一致
            # （Requirement 状态已 commit 为 final_status，但 FP 保存失败，需回滚）
            try:
                async with async_session() as rollback_s:
                    await rollback_s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(status=original_status))
                    await rollback_s.commit()
            except Exception as revert_err:
                logger.warning("[Task/Regenerate] FP失败状态回滚失败: req_id={}, error={}", req_id, str(revert_err)[:150])
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

        # 甲3: result 携带质量门禁警告
        regen_result_json: dict = {
            "req_id": req_id, "new_title": new_title, "status": final_status,
            "version_count": len(version_history),
            "needs_clarification": needs_clarify,
            "question_count": len(questions),
        }
        # v3.5.7（P1-A）：复用副本透出（用户可见「哪些 FP 是复用基线」）
        if captured_reused_fps:
            regen_result_json["reused_fps"] = [
                {"name": r.get("name", ""), "source_fp_id": r.get("source_fp_id", "")}
                for r in captured_reused_fps if isinstance(r, dict)
            ]
        if regen_quality_warning:
            regen_result_json["quality_warning"] = regen_quality_warning
        # v3.4.6（缺陷 B）：FP 提取失败降级透出（与 _run_requirement_analysis 对称）。
        _apply_fp_extract_degrade(regen_result_json, fp_extract_degraded, fp_extract_error)
        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message=f"需求分析已重新生成，进入{status_desc}",
            result_json=regen_result_json,
            completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task/Regenerate] 重新生成完成: task_id={}, req_id={}", task_id, req_id)

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task/Regenerate] 重新生成失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        # v3.4.6（缺陷 B）：失败路径同样透出 FP 提取降级标记（与另外两个入口对称）。
        regen_failed_result: dict = {}
        _apply_fp_extract_degrade(
            regen_failed_result, fp_extract_degraded, fp_extract_error,
            reason=_FP_DEGRADE_REASON_FAILED,
        )
        await _update_task(
            task_id, status="failed", error=err_msg,
            message="重新生成失败: " + err_msg[:200],
            completed_at=datetime.now(timezone.utc),
            result_json=regen_failed_result or None,
        )
        # 失败时回退到原始状态（v2.8.52: 不再无条件升级为 pending_review）
        try:
            async with async_session() as s:
                await s.execute(sa_update(Requirement).where(Requirement.id == req_id).values(status=original_status))
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
- business_rules / data_constraints / preconditions：合并去重
- v2.48: 不再输出 acceptance_criteria（已合并到 business_rules，type=validation/business）
- v2.48: 不再输出 test_hints（测试提示由用例生成阶段自行推导）
- v2.48: data_constraints.type 必须使用中文（字符串/整数/小数/布尔/日期等）

原始功能点：
{json.dumps(fp_jsons, ensure_ascii=False, indent=2)}

请输出合并后的功能点 JSON。子 SRR 必须使用标准格式。"""

        await _update_task(task_id, progress=30, message="LLM 正在合并功能点...")
        merged = await llm_client.call_json(
            prompt=merge_prompt,
            system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
            schema_hint={"name": str, "fp_type": str, "summary": str, "suggested_module": str, "sub_srr": dict},
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
            # v2.45 修复：合并后从 cross_fp_interactions 重建 FPDependency，避免合并 FP 丢失全部依赖
            try:
                from sqlalchemy import delete as _sa_del_dep
                # 获取合并后该需求下所有 FP ID
                fp_ids_result = await s.execute(
                    _sa_sel(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
                )
                cur_fp_ids = [r[0] for r in fp_ids_result.all()]
                if cur_fp_ids:
                    # 清理旧依赖（仅活跃的，不清理已冻结的版本快照）
                    await s.execute(
                        _sa_del_dep(FPDependency).where(
                            FPDependency.source_fp_id.in_(cur_fp_ids),
                            FPDependency.target_fp_id.in_(cur_fp_ids),
                            FPDependency.is_active == True,  # noqa: E712
                        )
                    )
                    # 从 cross_fp_interactions 提取依赖关系重建
                    req_srr_result = await s.execute(
                        _sa_sel(Requirement.srr_json).where(Requirement.id == req_id)
                    )
                    req_srr_row = req_srr_result.first()
                    if req_srr_row and req_srr_row[0]:
                        cur_srr = req_srr_row[0] if isinstance(req_srr_row[0], dict) else {}
                        cur_cfi = cur_srr.get("cross_fp_interactions", {})
                        if isinstance(cur_cfi, dict):
                            cur_flows = cur_cfi.get("cross_fp_flows", [])
                            new_deps = []
                            for flow in (cur_flows if isinstance(cur_flows, list) else []):
                                if not isinstance(flow, dict):
                                    continue
                                src = flow.get("source_fp", "")
                                tgt = flow.get("target_fp", "")
                                if src and tgt and src != tgt:
                                    new_deps.append({
                                        "source_fp_name": src,
                                        "target_fp_name": tgt,
                                        "relation_type": flow.get("relation_type", "depends_on"),
                                        "description": flow.get("description", ""),
                                    })
                            rebuilt = await _save_fp_dependencies(s, req_id, new_deps)
                            logger.info(
                                "[FP-Merge] FPDependency 重建: req_id={}, 新建 {} 条依赖",
                                req_id, rebuilt,
                            )
            except Exception as rebuild_e:
                logger.warning(
                    "[FP-Merge] FPDependency 重建失败（不阻断合并）: req_id={}, error={}",
                    req_id, str(rebuild_e)[:200],
                )
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

