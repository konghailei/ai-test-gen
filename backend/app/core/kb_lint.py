# -*- coding: utf-8 -*-
"""知识库编译层 Lint 周期自检（v3.5.0 / docs/18 P2）。

## 为什么需要

LLM Wiki 编译模式的最大软肋是 **"everyone compiles, nobody recompiles"**——
编译快照没有构建系统盯着源，源变了编译产物不会跟着变，而且编译产物读起来
像参考资料，带着"隐含的时效性承诺"，腐化比普通笔记更危险。

本系统的天然优势：归档事件就是构建触发器（consolidate_crsr 在归档/纠错链路）。
但触发器会漏发（consolidate 失败不阻断主流程，属既有设计）——Lint 补的就是
"触发器漏发/失败后的漂移检测"。

## 做什么 / 不做什么

- ✅ **只读扫描 + 报告**：三项检查全部只读，结果写日志（warning 级，运维可见）
- ❌ **不自动修复**：修复动作（重新 consolidate + 出箱 upsert）需人工确认后执行
  ——删除/重写不可逆，与 kb_maintenance / reconcile_cases 的"默认只报告"纪律一致

## 三项检查

| 查 | 逻辑 | 输出 |
|---|---|---|
| L1 CRSR 漂移 | 重算 aggregate_crsr 与固化行 content 逐字段比对 | 漂移清单（product/fp/差异字段） |
| L2 跨版本规则矛盾 | 同名 FP 各版本 business_rules 同 id 不同内容 / data_constraints 同语义键不同约束 | 矛盾清单（谁与谁冲突、来自哪些版本） |
| L3 CRSR 向量缺失 | 固化行存在但 Milvus 无对应向量（出箱丢失/failed） | 缺失向量清单（修复 = 重新 enqueue） |

## 配置

`KB_LINT_ENABLED`（默认 True）/ `KB_LINT_INTERVAL`（默认 86400 秒 = 24h）
"""
from __future__ import annotations

import asyncio
import json

from loguru import logger

_loop_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# 纯函数部分（离线可测）
# ---------------------------------------------------------------------------

def _norm_text(s) -> str:
    """业务规则文本规范化（去空白与中英文标点）——与 fp_crsr._norm_text 同口径，
    此处复制语义避免循环导入（lint 需要 fp_crsr 的聚合，fp_crsr 不需要 lint）。"""
    import re
    if not isinstance(s, str):
        return ""
    return re.sub(r"[\s\u3000「」【】()（）:：,，.。;；、\-—~～]", "", s)


def detect_crsr_drift(consolidated: dict, recomputed: dict) -> list[str]:
    """L1：比对固化行与重算结果，返回差异字段列表（纯函数）。

    - 标量/嵌套结构：规范化 JSON 比对（键序无关）
    - 只报告差异字段名，不比较"谁新谁旧"（重算结果恒为新真值）
    """
    if not isinstance(consolidated, dict) or not isinstance(recomputed, dict):
        return ["<non-dict>"]
    drift = []
    all_keys = set(consolidated) | set(recomputed)
    for k in sorted(all_keys):
        a, b = consolidated.get(k), recomputed.get(k)
        try:
            same = json.dumps(a, ensure_ascii=False, sort_keys=True) == \
                   json.dumps(b, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001
            same = a == b
        if not same:
            drift.append(k)
    return drift


def detect_rule_contradictions(versioned_srrs: list[dict]) -> list[dict]:
    """L2：跨版本业务规则/数据约束矛盾检测（纯函数）。

    Args:
        versioned_srrs: [{req_id, srr}, ...]（同名 FP 的各版本，顺序不限）

    Returns:
        矛盾清单: [{kind, key, versions: [{req_id, value}], note}]
        - kind="rule_id": 同 business_rules.id 不同 rule 文本
        - kind="constraint": 同 data_constraints 语义键（field+type+required）不同 constraints
    """
    contradictions: list[dict] = []

    # business_rules 按 id 聚合
    by_rule_id: dict[str, list[tuple[str, str]]] = {}
    by_constraint_key: dict[str, list[tuple[str, str]]] = {}
    for item in versioned_srrs:
        req_id = str(item.get("req_id", ""))
        srr = item.get("srr") or {}
        if not isinstance(srr, dict):
            continue
        for r in (srr.get("business_rules") or []):
            if isinstance(r, dict) and r.get("id"):
                rule_text = _norm_text(r.get("rule"))
                by_rule_id.setdefault(str(r["id"]), []).append((req_id, rule_text))
        for c in (srr.get("data_constraints") or []):
            if isinstance(c, dict) and c.get("field"):
                sem_key = "|".join(str(c.get(k, "")) for k in ("field", "type", "required"))
                cons_text = _norm_text(c.get("constraints"))
                by_constraint_key.setdefault(sem_key, []).append((req_id, cons_text))

    for rid, versions in by_rule_id.items():
        distinct = {t for _, t in versions if t}
        if len(distinct) > 1:
            contradictions.append({
                "kind": "rule_id", "key": rid,
                "versions": [{"req_id": q, "value": t} for q, t in versions],
                "note": f"同 id 规则在不同版本中内容不一致（{len(distinct)} 种表述）",
            })
    for ck, versions in by_constraint_key.items():
        distinct = {t for _, t in versions if t}
        if len(distinct) > 1:
            contradictions.append({
                "kind": "constraint", "key": ck,
                "versions": [{"req_id": q, "value": t} for q, t in versions],
                "note": f"同字段约束在不同版本中不一致（{len(distinct)} 种约束）",
            })
    return contradictions


# ---------------------------------------------------------------------------
# 扫描器（只读，访问 DB / Milvus）
# ---------------------------------------------------------------------------

async def scan_crsr_drift(limit: int = 500) -> list[dict]:
    """L1 扫描：全表比对固化 CRSR vs 实时重算。只读。"""
    from sqlalchemy import select
    from app.db.database import async_session
    from app.db.models import FunctionalPointCRSR
    from app.core.fp_crsr import aggregate_crsr

    drifts: list[dict] = []
    async with async_session() as session:
        rows = (await session.execute(
            select(FunctionalPointCRSR).limit(limit)
        )).scalars().all()
        for row in rows:
            try:
                live = await aggregate_crsr(session, row.product_id, row.fp_name)
                if live["version_count"] == 0:
                    continue  # 源 FP 全删：交给 L3/人工判断，不算漂移
                diff = detect_crsr_drift(row.content or {}, live["crsr"])
                if diff:
                    drifts.append({
                        "product_id": row.product_id, "fp_name": row.fp_name,
                        "drift_fields": diff,
                    })
            except Exception as e:  # noqa: BLE001
                logger.warning("[KbLint] L1 比对失败（跳过）: {}/{}, err={}",
                               row.product_id, row.fp_name, str(e)[:120])
    return drifts


async def scan_rule_contradictions(limit: int = 500) -> list[dict]:
    """L2 扫描：同名 FP 各版本的规则矛盾。只读。"""
    from sqlalchemy import select
    from app.db.database import async_session
    from app.db.models import FunctionalPoint, Requirement
    from collections import defaultdict

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    async with async_session() as session:
        rows = (await session.execute(
            select(
                FunctionalPoint.srr_json, FunctionalPoint.name,
                Requirement.id, Requirement.product_id,
            ).join(Requirement, Requirement.id == FunctionalPoint.requirement_id).limit(limit)
        )).all()
        for srr, fp_name, req_id, product_id in rows:
            if fp_name:
                groups[(product_id, fp_name)].append({"req_id": req_id, "srr": srr or {}})

    contradictions: list[dict] = []
    for (product_id, fp_name), versions in groups.items():
        if len(versions) < 2:
            continue  # 单版本无"跨版本矛盾"可言
        for c in detect_rule_contradictions(versions):
            contradictions.append({"product_id": product_id, "fp_name": fp_name, **c})
    return contradictions


async def scan_crsr_vector_missing(limit: int = 500) -> list[dict]:
    """L3 扫描：固化 CRSR 行在 Milvus 无对应向量（出箱丢失/failed）。只读。"""
    from sqlalchemy import select
    from app.db.database import async_session
    from app.db.models import FunctionalPointCRSR
    from app.db.milvus_store import vector_store
    from app.core.fp_crsr import crsr_vector_id

    missing: list[dict] = []

    def _existing_ids(ids: list[str]) -> set[str]:
        col = vector_store._get_collection("requirements")
        col.load()
        # ⚠️ 用 id in [...]：Milvus 对非 ASCII id 的 == 等值查询会静默返回 0（既有陷阱）
        rows = col.query(expr=f'id in {json.dumps(ids)}', output_fields=["id"])
        return {r["id"] for r in rows}

    async with async_session() as session:
        rows = (await session.execute(
            select(FunctionalPointCRSR.product_id, FunctionalPointCRSR.fp_name).limit(limit)
        )).all()
    pairs = [(p, n) for p, n in rows]
    if not pairs:
        return missing
    ids = [crsr_vector_id(p, n) for p, n in pairs]
    try:
        found = await asyncio.to_thread(_existing_ids, ids)
    except Exception as e:  # noqa: BLE001
        logger.warning("[KbLint] L3 向量查询失败（本轮跳过）: {}", str(e)[:150])
        return missing
    for (p, n), vid in zip(pairs, ids):
        if vid not in found:
            missing.append({"product_id": p, "fp_name": n, "vector_id": vid})
    return missing


async def run_lint_once(*, report_only: bool = True) -> dict:
    """执行一轮三查（只读），返回报告并写日志。异常不外抛（fail-open，下轮重试）。"""
    report = {"drift": [], "contradictions": [], "vector_missing": [], "errors": []}

    try:
        report["drift"] = await scan_crsr_drift()
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"L1: {str(e)[:150]}")
    try:
        report["contradictions"] = await scan_rule_contradictions()
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"L2: {str(e)[:150]}")
    try:
        report["vector_missing"] = await scan_crsr_vector_missing()
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"L3: {str(e)[:150]}")

    n_issues = len(report["drift"]) + len(report["contradictions"]) + len(report["vector_missing"])
    if n_issues or report["errors"]:
        logger.warning(
            "[KbLint] 本轮自检发现 {} 项待处理（漂移 {} / 矛盾 {} / 向量缺失 {} / 扫描错误 {}）——"
            "**只报告未修复**，修复需人工确认",
            n_issues, len(report["drift"]), len(report["contradictions"]),
            len(report["vector_missing"]), len(report["errors"]),
        )
        for d in report["drift"][:5]:
            logger.warning("[KbLint] L1 漂移: {}/{} 字段={}",
                           d["product_id"], d["fp_name"], d["drift_fields"])
        for c in report["contradictions"][:5]:
            logger.warning("[KbLint] L2 矛盾: {}/{} kind={} key={}",
                           c["product_id"], c["fp_name"], c["kind"], c["key"])
        for m in report["vector_missing"][:5]:
            logger.warning("[KbLint] L3 缺失: {}/{} vector_id={}",
                           m["product_id"], m["fp_name"], m["vector_id"])
    else:
        logger.info("[KbLint] 本轮自检全部一致（无漂移/矛盾/缺失）")
    return report


# ---------------------------------------------------------------------------
# 周期任务（与 kb_maintenance 同模板）
# ---------------------------------------------------------------------------

async def _lint_loop(interval: int) -> None:
    logger.info("[KbLint] 知识库编译层自检任务已启动: interval={}s", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await run_lint_once()
        except asyncio.CancelledError:
            logger.info("[KbLint] 知识库编译层自检任务已停止")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[KbLint] 自检循环异常（下轮重试）: {}", str(e)[:200])


def start_kb_lint() -> asyncio.Task | None:
    """启动 lint 周期任务（lifespan 调用）。"""
    global _loop_task
    from app.config import settings

    if _loop_task is not None and not _loop_task.done():
        return _loop_task
    if not getattr(settings, "KB_LINT_ENABLED", True):
        logger.info("[KbLint] 已禁用（KB_LINT_ENABLED=False），不启动")
        return None
    interval = int(getattr(settings, "KB_LINT_INTERVAL", 86400) or 86400)
    if interval <= 0:
        logger.info("[KbLint] 间隔非正数，不启动")
        return None
    _loop_task = asyncio.create_task(_lint_loop(interval))
    return _loop_task


async def stop_kb_lint() -> None:
    global _loop_task
    if _loop_task is None:
        return
    _loop_task.cancel()
    try:
        await _loop_task
    except asyncio.CancelledError:
        pass
    _loop_task = None
    logger.info("[KbLint] 自检任务已停止")
