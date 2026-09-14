# -*- coding: utf-8 -*-
"""孤儿向量 GC（N1 治理设计 B6-2）。

背景
----
docs/14 §9.2 环①：需求/用例在 PG 中被删除后，**Milvus 里的向量没有同步清理**，
这些"幽灵文档"会占满 KB 检索的 top-K 名额。而 `knowledge_qa.py:116` 的孤儿过滤
是**先召回后过滤**，名额已被占掉，过滤后真实文档所剩无几——直接导致答案只能依据
AI 生成的测试用例（N1 的表现）。

实测（2026-09-14）：按产品过滤检索 10 条，其中 **9 条的 REQ ID 不在 requirements 表**。

安全设计（重要）
--------------
- **默认 dry-run**，只出清单，不删任何东西
- 只有显式传 `--apply` 才真正删除
- 删除**只针对 PG 中确认不存在的 ID**，不做模糊匹配
- 删除前再打印一次待删清单并计入日志

用法（容器内执行）
----------------
    docker cp scripts/kb_orphan_gc.py ai-test-gen-api:/app/_gc.py
    # 1) 先 dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gc.py
    # 2) 人工确认清单后，才执行
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gc.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select, text

from app.db.database import async_session

OUT = "/app/_e2e_tmp/artifacts/kb_orphan_gc_report.json"
BATCH = 4096          # Milvus query 单页上限
MAX_PAGES = 50        # 安全阀：最多扫 50 页（≈20 万条）

# 集合 key -> 说明
TARGETS = {
    "requirements": "需求 / 功能点 / KB 问答（混合集合）",
    "test_cases": "测试用例",
    "patterns": "测试模式",
}

FP_PREFIX = "FP-"   # 功能点向量主键 = "FP-" + functional_points.id


async def _pg_id_set(sql: str) -> set[str]:
    async with async_session() as s:
        rows = (await s.execute(text(sql))).scalars().all()
    return {str(r) for r in rows}


async def build_valid_ids() -> dict[str, set[str]]:
    """按 metadata.type 分别构造合法 ID 集合。

    ⚠️ 这里必须按 type 分类，不能拿整个 requirements 集合直接和 requirements 表比对：
    该集合是**混合集合**（实测含 requirement 97 / functional_point 33 / knowledge_qa 1），
    且功能点向量主键带 `FP-` 前缀而 `functional_points.id` 不带——
    直接比对会把 33 条合法功能点向量误判为孤儿（2026-09-14 dry-run 时发现的）。
    """
    return {
        "requirement": await _pg_id_set("SELECT id FROM requirements"),
        "functional_point": await _pg_id_set("SELECT id FROM functional_points"),
        "knowledge_qa": await _pg_id_set("SELECT id FROM knowledge_qa"),
        "test_case": await _pg_id_set("SELECT id FROM test_cases"),
        "pattern": await _pg_id_set("SELECT id FROM test_patterns"),
    }


def classify(vec_id: str, meta: dict, valid: dict[str, set[str]]) -> tuple[str, bool]:
    """返回 (归类, 是否合法)。

    ⚠️ 判定采用**全局存在性**，而非"按 metadata.type 查对应表"。

    原因（2026-09-14 实测踩到）：`REQ-1788854035042-8100` 的向量 metadata
    `type="knowledge_qa"`，但**同一 ID 也存在于 `requirements` 表**（archived）。
    若按 type 只查 knowledge_qa 表 → 判为孤儿 → **会误删一条活需求的向量**。

    因此：只要该 ID 在**任一**候选表（requirements / functional_points /
    knowledge_qa / test_cases / patterns）中存在，就不算孤儿。
    功能点向量额外剥离 `FP-` 前缀后再判一次。
    未知类型仍保守判为合法（不删）。
    """
    vtype = (meta or {}).get("type", "")
    base = vec_id[len(FP_PREFIX):] if vec_id.startswith(FP_PREFIX) else vec_id
    for name in ("requirement", "functional_point", "knowledge_qa",
                 "test_case", "pattern"):
        ids = valid.get(name, set())
        if vec_id in ids or base in ids:
            return vtype, True
    if not vtype or vtype in ("unknown",):
        return "unknown", True   # 保守：无法归类的一律不动
    return vtype, False


def milvus_records(coll_key: str) -> list[tuple[str, dict]]:
    """分页取出集合内全部 (id, metadata)。"""
    from app.db.milvus_store import vector_store

    vector_store._ensure_connected()
    coll = vector_store._get_collection(coll_key)
    out: list[tuple[str, dict]] = []
    for page in range(MAX_PAGES):
        try:
            res = coll.query(
                expr="",
                output_fields=["id", "metadata"],
                limit=BATCH,
                offset=page * BATCH,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 第 {page} 页查询失败（可能不支持 offset）: {str(e)[:100]}")
            break
        if not res:
            break
        out.extend((str(r["id"]), r.get("metadata") or {}) for r in res)
        if len(res) < BATCH:
            break
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真正执行删除；不传则只 dry-run 出清单")
    args = ap.parse_args()

    from app.db.milvus_store import vector_store

    valid = await build_valid_ids()
    print("PG 合法 ID 数：", {k: len(v) for k, v in valid.items()})

    report: dict = {"apply": args.apply, "collections": {}}

    for coll_key, label in TARGETS.items():
        print(f"\n=== {coll_key}（{label}）===")
        try:
            recs = await asyncio.to_thread(milvus_records, coll_key)
        except Exception as e:  # noqa: BLE001
            print(f"  跳过：读取集合失败 {str(e)[:120]}")
            continue

        by_kind: dict[str, int] = {}
        orphans: list[str] = []
        for vid, meta in recs:
            kind, ok = classify(vid, meta, valid)
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if not ok:
                orphans.append(vid)

        print(f"  向量 {len(recs)} 条，类型分布 {by_kind}")
        print(f"  → 孤儿 {len(orphans)} 条 ({len(orphans) / max(len(recs), 1):.1%})")

        report["collections"][coll_key] = {
            "label": label,
            "vector_count": len(recs),
            "kind_distribution": by_kind,
            "orphan_count": len(orphans),
            "orphan_ratio": round(len(orphans) / max(len(recs), 1), 4),
            "orphan_ids": orphans[:500],
            "orphan_ids_truncated": len(orphans) > 500,
            "deleted": 0,
        }

        if orphans and args.apply:
            print(f"  ⚠️ 即将删除 {len(orphans)} 条孤儿向量：")
            for o in orphans[:10]:
                print(f"      {o}")
            if len(orphans) > 10:
                print(f"      ... 另 {len(orphans) - 10} 条")
            deleted = 0
            for oid in orphans:
                try:
                    await asyncio.to_thread(
                        lambda _id=oid: vector_store._get_collection(coll_key).delete(
                            expr=f'id == "{_id}"'
                        )
                    )
                    deleted += 1
                except Exception as e:  # noqa: BLE001
                    print(f"      [fail] {oid}: {str(e)[:80]}")
            try:
                vector_store._get_collection(coll_key).flush()
            except Exception:  # noqa: BLE001
                pass
            print(f"  ✅ 已删除 {deleted}/{len(orphans)}")
            report["collections"][coll_key]["deleted"] = deleted

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    total = sum(c.get("orphan_count", 0) for c in report["collections"].values())
    print(f"\n合计孤儿向量 {total} 条；模式={'APPLY（已删除）' if args.apply else 'DRY-RUN（未删除）'}")
    print(f"产物: {OUT}")
    if not args.apply and total:
        print("确认清单无误后，加 --apply 再跑一次才会真正删除。")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
