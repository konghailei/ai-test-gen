# -*- coding: utf-8 -*-
"""向量化状态对账 / 自愈（N5-c，docs/14 §9.10）。

背景
----
N5 追查发现：`requirements.vectorization_status` 与实际 Milvus 向量**可能不一致**——
`REQ-1788322894962-6696`（DevBlog V1.1）PG 标 `completed`，Milvus 中却**没有任何向量**。
成因无法从日志确证（操作日志仅剩 3 条），但暴露了三个可确证的机制缺陷：

    N5-a 归档派发 Celery 向量化任务后无终态保证（有需求长期停留 pending）
    N5-b KB 纠错「删旧向量 → 重新嵌入」非原子，中断则永久丢向量且状态不回滚
    N5-c 无「PG 状态 vs Milvus 实际」对账机制 → 这类不一致无法被自动发现

本工具即 N5-c：**同时兜住 N5-a / N5-b**，且不依赖定位历史成因。

安全设计
--------
- **默认 dry-run**，只报告不一致，不写任何数据
- `--repair` 仅执行**新增**（补写缺失向量），**绝不删除**
- 补写使用归档同款 metadata 构造，并**保留原 status**（不伪造 archived）
- 补写成功后将 `vectorization_status` 回写为 `completed`；失败置 `failed` 并记录原因

用法（容器内执行）
----------------
    docker cp scripts/kb_vector_reconcile.py ai-test-gen-api:/app/_rec.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _rec.py            # dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _rec.py --repair   # 补写
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from sqlalchemy import select, text

from app.db.database import async_session
from app.db.models import Project, Requirement

OUT = "/app/_e2e_tmp/artifacts/kb_vector_reconcile.json"


def _fetch_all_vector_ids() -> set[str]:
    from app.db.milvus_store import vector_store

    vector_store._ensure_connected()
    coll = vector_store._get_collection("requirements")
    ids: set[str] = set()
    for page in range(50):
        try:
            res = coll.query(expr="", output_fields=["id"], limit=4096, offset=page * 4096)
        except Exception:  # noqa: BLE001
            break
        if not res:
            break
        ids.update(str(r["id"]) for r in res)
        if len(res) < 4096:
            break
    return ids


def _rebuild(req_id: str, srr_json: dict, meta: dict) -> None:
    from app.db.milvus_store import vector_store

    vector_store.add_requirement(req_id, srr_json or {}, metadata=meta)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", action="store_true",
                    help="补写缺失向量；不传则只报告")
    args = ap.parse_args()

    async with async_session() as s:
        reqs = (await s.execute(
            select(Requirement.id, Requirement.status, Requirement.product_id,
                   Requirement.project_id, Requirement.priority, Requirement.modules,
                   Requirement.srr_json, Requirement.vectorization_status,
                   Requirement.title)
        )).all()
        projects = {p[0]: p[1] for p in (await s.execute(
            select(Project.id, Project.version)
        )).all()}

    vec_ids = await asyncio.to_thread(_fetch_all_vector_ids)
    print(f"Milvus requirements 集合向量数: {len(vec_ids)}；PG 需求数: {len(reqs)}")

    missing, stuck_pending, ok = [], [], []
    for r in reqs:
        rid, status, prod, proj, prio, mods, srr, vs, title = r
        present = rid in vec_ids
        if vs == "completed" and not present:
            # 标完成但向量不在 → 需补写
            missing.append({"id": rid, "status": status, "title": title,
                            "product_id": prod, "project_id": proj, "reason": "completed_but_missing"})
        elif vs in ("pending", "processing") and not present:
            # N5-a：派发后长期停留 pending/processing 且向量确实不在 → 同样需补写
            # （向量已在的 pending 只属状态未回写，单独归一类，避免重复写向量）
            stuck_pending.append({"id": rid, "status": status, "title": title,
                                  "product_id": prod, "project_id": proj,
                                  "reason": f"{vs}_and_missing"})
        elif vs in ("pending", "processing") and present:
            stuck_pending.append({"id": rid, "status": status, "title": title,
                                  "product_id": prod, "project_id": proj,
                                  "reason": f"{vs}_but_present"})
        elif vs == "completed" and present:
            ok.append(rid)

    print(f"\n=== 对账结果 ===")
    print(f"  ✅ 标 completed 且真有向量: {len(ok)}")
    print(f"  ❌ 标 completed 但无向量（需补写）: {len(missing)}")
    for m in missing:
        print(f"       {m['id']}  status={m['status']}  {str(m['title'])[:30]}")
    print(f"  ⚠️ 卡在中间态（N5-a）: {len(stuck_pending)}")
    for m in stuck_pending:
        print(f"       {m['id']}  status={m['status']}  reason={m['reason']}  "
              f"{str(m['title'])[:26]}")

    # 需要补写向量的 = completed 缺失 + pending/processing 且向量确实缺失
    to_repair = list(missing) + [m for m in stuck_pending
                                 if m["reason"].endswith("_and_missing")]
    # 仅需修正状态（向量已存在，只是 Celery 未回写终态）——**不重写向量**
    status_only = [m for m in stuck_pending if m["reason"].endswith("_but_present")]

    if status_only:
        fixed_status = 0
        if args.repair:
            for m in status_only:
                try:
                    async with async_session() as s:
                        await s.execute(text(
                            "UPDATE requirements SET vectorization_status='completed', "
                            "vectorization_error=NULL WHERE id=:i"
                        ), {"i": m["id"]})
                        await s.commit()
                    fixed_status += 1
                    print(f"  ✅ 修正状态 pending→completed（向量本已存在）: {m['id']}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ 状态修正失败 {m['id']}: {str(e)[:100]}")
        report_status_fixed = fixed_status
    else:
        report_status_fixed = 0

    repaired, failed = 0, []
    if args.repair and to_repair:
        by_id = {r[0]: r for r in reqs}
        for m in to_repair:
            rid = m["id"]
            row = by_id[rid]
            srr_json = row[6] or {}
            meta = {
                "type": "requirement",
                "product_id": row[2],
                "project_id": row[3] or "",
                "modules": ",".join(row[5] or []),
                "priority": row[4],
                # ⚠️ 保留真实 status，**不**伪造成 archived——否则会绕过
                # 检索侧的 status 过滤，等于偷偷改变检索语义
                "status": row[1],
                "title": row[8],
            }
            try:
                await asyncio.to_thread(_rebuild, rid, srr_json, meta)
                async with async_session() as s:
                    await s.execute(text(
                        "UPDATE requirements SET vectorization_status='completed', "
                        "vectorization_error=NULL WHERE id=:i"
                    ), {"i": rid})
                    await s.commit()
                repaired += 1
                print(f"  ✅ 已补写向量: {rid}")
            except Exception as e:  # noqa: BLE001
                failed.append({"id": rid, "error": str(e)[:200]})
                async with async_session() as s:
                    await s.execute(text(
                        "UPDATE requirements SET vectorization_status='failed', "
                        "vectorization_error=:e WHERE id=:i"
                    ), {"i": rid, "e": str(e)[:500]})
                    await s.commit()
                print(f"  ❌ 补写失败: {rid} — {str(e)[:100]}")

    report = {
        "mode": "repair" if args.repair else "dry-run",
        "vector_count": len(vec_ids),
        "pg_requirement_count": len(reqs),
        "completed_with_vector": len(ok),
        "completed_without_vector": missing,
        "stuck_intermediate": stuck_pending,
        "to_repair": [m["id"] for m in to_repair],
        "repaired": repaired,
        "status_only_fixed": report_status_fixed,
        "repair_failed": failed,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n产物: {OUT}")
    if not args.repair and missing:
        print("确认清单后加 --repair 才补写（只新增，不删除）。")


if __name__ == "__main__":
    asyncio.run(main())
