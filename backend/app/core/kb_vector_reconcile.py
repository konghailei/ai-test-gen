"""KB 向量对账 / 自愈（N5-c）与孤儿向量扫描（B6-2）核心逻辑。

为什么放在 `app/core` 而不是 `scripts/`
------------------------------------
同一套逻辑有两个消费方，放这里避免两份实现漂移：
1. **应用内周期任务**（`app/core/kb_maintenance.py`）——自动修复，兜住 N5-a/N5-b
2. **命令行工具**（`scripts/kb_vector_reconcile.py`、`scripts/kb_orphan_gc.py`）——手动排查

安全原则（务必保持）
------------------
- **修复只新增不删除**：只做「补写缺失向量」与「修正状态」，绝不删向量
- 补写时 metadata **保留真实 `status`**，不伪造成 `archived`
  （否则会绕过检索侧的 status 过滤，等于偷偷改变检索语义）
- 孤儿扫描**默认只报告**，删除必须由人显式确认（见 `kb_orphan_gc.py --apply`）

背景（详见 docs/14 §9.10 / §9.17）
--------------------------------
- N5-a：归档派发 Celery 向量化任务后无终态保证——有的需求长期停留 `pending`
- N5-b：KB 纠错「删旧向量 → 重新嵌入」非原子，中断则永久丢向量且状态不回滚
- N5-c：**无「PG 状态 vs Milvus 实际」对账机制**，上述不一致无法被自动发现
- 实测：`REQ-1788322894962-6696` 标 `completed` 但 Milvus 中无任何向量；
  另有 3 条长期 `pending`（后查明**向量其实都在**，只是状态未回写）
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.db.database import async_session

# 分页参数：Milvus query 单页上限
_PAGE = 4096
_MAX_PAGES = 50

# 向量主键前缀：功能点向量 = "FP-" + functional_points.id
_FP_PREFIX = "FP-"


def _fetch_all_vector_ids(coll_key: str = "requirements") -> set[str]:
    """分页取出集合内全部主键 ID。"""
    from app.db.milvus_store import vector_store

    vector_store._ensure_connected()
    coll = vector_store._get_collection(coll_key)
    ids: set[str] = set()
    for page in range(_MAX_PAGES):
        try:
            res = coll.query(expr="", output_fields=["id"], limit=_PAGE,
                             offset=page * _PAGE)
        except Exception as e:  # noqa: BLE001
            logger.warning("[KbReconcile] 分页查询失败（可能不支持 offset）: {}", str(e)[:100])
            break
        if not res:
            break
        ids.update(str(r["id"]) for r in res)
        if len(res) < _PAGE:
            break
    return ids


async def reconcile_requirements(repair: bool = True) -> dict[str, Any]:
    """对账 `requirements` 表与 Milvus 中的需求向量，并按需自愈。

    Args:
        repair: True 时执行修复（补写向量 / 修正状态）；False 仅报告。

    Returns:
        dict: 含四类统计与修复计数。
    """
    from sqlalchemy import select, text

    from app.db.milvus_store import vector_store
    from app.db.models import Requirement

    async with async_session() as s:
        reqs = (await s.execute(
            select(Requirement.id, Requirement.status, Requirement.product_id,
                   Requirement.project_id, Requirement.priority, Requirement.modules,
                   Requirement.srr_json, Requirement.vectorization_status,
                   Requirement.title)
        )).all()

    vec_ids = await asyncio.to_thread(_fetch_all_vector_ids, "requirements")

    missing: list[dict] = []          # 标 completed 但无向量 → 需补写
    need_vector: list[dict] = []      # 卡 pending/processing 且无向量 → 需补写
    status_only: list[dict] = []      # 卡 pending/processing 但向量已在 → 仅改状态
    ok = 0

    for r in reqs:
        rid, status, prod, proj, prio, mods, srr, vs, title = r
        present = rid in vec_ids
        if vs == "completed" and not present:
            missing.append({"id": rid, "status": status, "title": title})
        elif vs in ("pending", "processing") and not present:
            need_vector.append({"id": rid, "status": status, "title": title})
        elif vs in ("pending", "processing") and present:
            status_only.append({"id": rid, "status": status, "title": title})
        elif vs == "completed" and present:
            ok += 1

    to_repair = missing + need_vector
    repaired, failed, status_fixed = 0, [], 0

    if repair and to_repair:
        by_id = {r[0]: r for r in reqs}
        for m in to_repair:
            rid = m["id"]
            row = by_id[rid]
            meta = {
                "type": "requirement",
                "product_id": row[2],
                "project_id": row[3] or "",
                "modules": ",".join(row[5] or []),
                "priority": row[4],
                # ⚠️ 保留真实 status，不伪造成 archived
                "status": row[1],
                "title": row[8],
            }
            try:
                await asyncio.to_thread(
                    vector_store.add_requirement, rid, row[6] or {}, meta
                )
                async with async_session() as s:
                    await s.execute(text(
                        "UPDATE requirements SET vectorization_status='completed', "
                        "vectorization_error=NULL WHERE id=:i"
                    ), {"i": rid})
                    await s.commit()
                repaired += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"id": rid, "error": str(e)[:200]})
                try:
                    async with async_session() as s:
                        await s.execute(text(
                            "UPDATE requirements SET vectorization_status='failed', "
                            "vectorization_error=:e WHERE id=:i"
                        ), {"i": rid, "e": str(e)[:500]})
                        await s.commit()
                except Exception:  # noqa: BLE001
                    pass

    if repair and status_only:
        for m in status_only:
            try:
                async with async_session() as s:
                    await s.execute(text(
                        "UPDATE requirements SET vectorization_status='completed', "
                        "vectorization_error=NULL WHERE id=:i"
                    ), {"i": m["id"]})
                    await s.commit()
                status_fixed += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"id": m["id"], "error": f"status_only: {str(e)[:150]}"})

    return {
        "vector_count": len(vec_ids),
        "pg_requirement_count": len(reqs),
        "completed_with_vector": ok,
        "completed_without_vector": missing,
        "stuck_and_missing": need_vector,
        "stuck_but_present": status_only,
        "repaired": repaired,
        "status_only_fixed": status_fixed,
        "repair_failed": failed,
    }


async def scan_orphan_vectors(coll_key: str = "requirements") -> dict[str, Any]:
    """扫描孤儿向量（**只报告，不删除**）。

    判定采用**全局存在性**：只要 ID 在任一候选表中存在就不算孤儿。
    理由（实测踩坑）：`REQ-1788854035042-8100` 的向量 `metadata.type="knowledge_qa"`，
    但同一 ID 也存在于 `requirements` 表——若按 type 查对应表会误判为孤儿并**误删活需求向量**。
    """
    from sqlalchemy import text

    async with async_session() as s:
        valid: dict[str, set[str]] = {}
        for key, sql in (
            ("requirement", "SELECT id FROM requirements"),
            ("functional_point", "SELECT id FROM functional_points"),
            ("knowledge_qa", "SELECT id FROM knowledge_qa"),
            ("test_case", "SELECT id FROM test_cases"),
            ("pattern", "SELECT id FROM test_patterns"),
        ):
            valid[key] = {str(x) for x in
                          (await s.execute(text(sql))).scalars().all()}

    from app.db.milvus_store import vector_store

    vector_store._ensure_connected()
    coll = vector_store._get_collection(coll_key)
    recs: list[tuple[str, dict]] = []
    for page in range(_MAX_PAGES):
        try:
            res = coll.query(expr="", output_fields=["id", "metadata"],
                             limit=_PAGE, offset=page * _PAGE)
        except Exception:  # noqa: BLE001
            break
        if not res:
            break
        recs.extend((str(r["id"]), r.get("metadata") or {}) for r in res)
        if len(res) < _PAGE:
            break

    orphans: list[str] = []
    for vid, meta in recs:
        base = vid[len(_FP_PREFIX):] if vid.startswith(_FP_PREFIX) else vid
        if any(vid in ids or base in ids for ids in valid.values()):
            continue
        vtype = (meta or {}).get("type", "")
        if not vtype or vtype == "unknown":
            continue   # 无法归类的保守不删
        orphans.append(vid)

    return {
        "collection": coll_key,
        "vector_count": len(recs),
        "orphan_count": len(orphans),
        "orphan_ids": orphans[:500],
        "orphan_ids_truncated": len(orphans) > 500,
    }
