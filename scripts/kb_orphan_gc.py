# -*- coding: utf-8 -*-
"""孤儿向量 GC（N1 治理 B6-2）——命令行入口。

扫描逻辑已抽到 `app/core/kb_vector_reconcile.scan_orphan_vectors`，
与应用内周期任务**共用同一套判定**（避免两份实现漂移）。

背景
----
需求/用例在 PG 中被删除后，Milvus 里的向量没有同步清理，形成"幽灵文档"。
实测 `requirements` 集合中孤儿占比一度达 60%（143 条向量里 86 条）。

> ⚠️ **注意**：孤儿向量**不影响检索结果**——`knowledge_qa._enrich_sources`
> 会按 PG 存在性把它们丢弃。所以这是**卫生问题**（候选池干净 + 存储一致性），
> 不是「答案依据用例」的成因（B6-2 已由 P0 降级 P2）。

安全设计（重要）
--------------
- **默认 dry-run**，只出清单，不删任何东西
- 只有显式 `--apply` 才真正删除
- 判定采用**全局存在性**：ID 在任一候选表（requirements / functional_points /
  knowledge_qa / test_cases / patterns）中存在即不算孤儿；FP 向量额外剥离 `FP-` 前缀
- 无法归类的**保守不删**

> 踩坑记录：早期版本「按 metadata.type 查对应表」，导致 `REQ-1788854035042-8100`
> （向量 type=knowledge_qa，但同 ID 存在于 requirements 表）被误判为孤儿 →
> **会误删活需求的向量**。故改为全局存在性判定。

用法（容器内执行）
----------------
    docker cp scripts/kb_orphan_gc.py ai-test-gen-api:/app/_gc.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gc.py            # dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gc.py --apply    # 删除
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.core.kb_vector_reconcile import scan_orphan_vectors

OUT = "/app/_e2e_tmp/artifacts/kb_orphan_gc_report.json"
COLLECTIONS = ("requirements", "test_cases", "patterns")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除；不传则只 dry-run 出清单")
    args = ap.parse_args()

    from app.db.milvus_store import vector_store

    report: dict = {"apply": args.apply, "collections": {}}
    total_orphans = 0

    for coll_key in COLLECTIONS:
        print(f"\n=== {coll_key} ===")
        try:
            res = await scan_orphan_vectors(coll_key)
        except Exception as e:  # noqa: BLE001
            print(f"  跳过：扫描失败 {str(e)[:120]}")
            continue

        n = res["orphan_count"]
        total_orphans += n
        print(f"  向量 {res['vector_count']} 条 → 孤儿 {n} 条 "
              f"({n / max(res['vector_count'], 1):.1%})")

        deleted = 0
        if n and args.apply:
            ids = res["orphan_ids"]
            print(f"  ⚠️ 即将删除 {n} 条：")
            for o in ids[:10]:
                print(f"      {o}")
            if n > 10:
                print(f"      ... 另 {n - 10} 条")
            coll = vector_store._get_collection(coll_key)
            for oid in ids:
                try:
                    await asyncio.to_thread(
                        lambda _id=oid, _c=coll: _c.delete(expr=f'id == "{_id}"')
                    )
                    deleted += 1
                except Exception as e:  # noqa: BLE001
                    print(f"      [fail] {oid}: {str(e)[:80]}")
            try:
                await asyncio.to_thread(coll.flush)
            except Exception:  # noqa: BLE001
                pass
            print(f"  ✅ 已删除 {deleted}/{n}")

        report["collections"][coll_key] = {**res, "deleted": deleted}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n合计孤儿向量 {total_orphans} 条；"
          f"模式={'APPLY（已删除）' if args.apply else 'DRY-RUN（未删除）'}")
    print(f"产物: {OUT}")
    if not args.apply and total_orphans:
        print("确认清单无误后，加 --apply 再跑一次才会真正删除。")


if __name__ == "__main__":
    asyncio.run(main())
