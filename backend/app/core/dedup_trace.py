"""去重留痕落库与一键恢复（v3.5.10 docs/32 B-6 闸门3）。

把去重判定从"日志/SSE 里飘过"升级为持久化记录（dedup_removals 表）：
- removed（剔除）：存完整 case_json，误删可一键恢复（error_cases 复活同型）；
- kept（保留判定）：闸门1 护栏拦截（reason 带命中维度）/ 闸门2 LLM 裁决
  非重复（reason=llm_distinct）——审计"为什么没删"。

失败语义：落库失败只记日志，绝不阻断生成主流程（与 review_reports 同款）。
"""

from __future__ import annotations

import time
import random

from app.utils.logger import logger

_seq = random.randint(0, 9999)


def _gen_removal_id() -> str:
    global _seq
    _seq = (_seq + 1) % 10000
    return f"DR-{int(time.time() * 1000)}-{_seq:04d}"


def build_removal_records(
    removed: list[dict],
    decisions: "list[dict] | None" = None,
    *,
    task_id: str = "",
    gen_batch_id: str = "",
    req_id: str = "",
    case_by_key: "dict | None" = None,
) -> list[dict]:
    """构建去重留痕记录（纯函数，供单测）。

    Args:
        removed: dedup_semantic/dedup_near_duplicates 的 removed 记录
            （含 removed/kept/title/reason；可含 case_json 完整用例）。
        decisions: decision_log（闸门1 拦截 / 闸门2 llm_distinct 的 kept 记录）。
        task_id / gen_batch_id / req_id: 归属标识。
        case_by_key: {用例标识: 完整 case dict}——removed 记录自身没带
            case_json 时的兜底数据源（从去重前全量用例集构建）。
    """
    records: list[dict] = []

    def _case_json(entry: dict) -> dict:
        cj = entry.get("case_json")
        if isinstance(cj, dict) and cj:
            return dict(cj)
        key = entry.get("removed") or ""
        found = (case_by_key or {}).get(key)
        return dict(found) if isinstance(found, dict) else {}

    for r in removed or []:
        if not isinstance(r, dict):
            continue
        records.append({
            "removal_id": _gen_removal_id(),
            "task_id": task_id,
            "gen_batch_id": gen_batch_id,
            "req_id": req_id,
            "decision": "removed",
            "removed_id": str(r.get("removed", ""))[:64],
            "removed_title": str(r.get("removed_title", ""))[:256],
            "kept_id": str(r.get("kept", ""))[:64],
            "kept_title": str(r.get("kept_title", ""))[:256],
            "reason": str(r.get("reason", ""))[:512],
            "removed_case_json": _case_json(r),
        })
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        records.append({
            "removal_id": _gen_removal_id(),
            "task_id": task_id,
            "gen_batch_id": gen_batch_id,
            "req_id": req_id,
            "decision": "kept",
            "removed_id": str(d.get("removed", ""))[:64],
            "removed_title": str(d.get("removed_title", ""))[:256],
            "kept_id": str(d.get("kept", ""))[:64],
            "kept_title": str(d.get("kept_title", ""))[:256],
            "reason": str(d.get("reason", ""))[:512],
            "removed_case_json": {},
        })
    return records


async def save_dedup_records(records: list[dict]) -> list[str]:
    """落库去重留痕（失败不阻断，返回已保存 removal_id 列表）。"""
    if not records:
        return []
    try:
        from app.db.database import async_session
        from app.db.models import DedupRemoval

        saved: list[str] = []
        async with async_session() as session:
            for r in records:
                row = DedupRemoval(**r)
                session.add(row)
                saved.append(r["removal_id"])
            await session.commit()
        logger.info("[DedupTrace] 去重留痕落库 {} 条", len(saved))
        return saved
    except Exception as e:  # noqa: BLE001
        logger.warning("[DedupTrace] 留痕落库失败（不阻断生成）: {}", str(e)[:200])
        return []


def slim_records_for_sse(records: list[dict]) -> list[dict]:
    """剔除 case_json（SSE/报告不背完整用例，落库才有恢复数据源）。"""
    return [
        {k: v for k, v in r.items() if k != "case_json" and k != "removed_case_json"}
        for r in records or []
    ]
