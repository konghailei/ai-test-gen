# -*- coding: utf-8 -*-
"""存量需求向量重建（v4.1.1 缺陷 T4 配套修复）。

为什么需要它
------------
T4 修复前，`_srr_to_text` 整段忽略 `cross_fp_interactions`，且只读 v2.34 已移除的
顶层 `business_rules`。**改代码不会修好数据**：存量 L0 向量里根本没有
「登录连续失败5次锁定账号30分钟」这类共享业务规则，知识问答检索依旧漏召
（Q2 实测 top-1 相似度 0.3896 < 0.50 阈值）。必须重嵌一次。

同时顺带修复 KB 元数据：`_srr_for_metadata` 此前白名单漏了 `cross_fp_interactions`，
重建会把带 CFI 的轻量 SRR 一起写回 metadata，澄清基线补全随之恢复。

安全设计
--------
- **默认 dry-run**：只报告「哪些向量会变、新增了哪些规则块」，不写库
- `--apply` 才真正重建；**不删除任何向量**，只做同 id 覆盖（先删后插，语义等价）
- 只处理 `doc_type == "requirement"` 的记录；不触碰 functional_point / test_case /
  pattern 等其它集合
- metadata 原样复用 Milvus 现有值（product_id / project_id / status / go_live_date…），
  确保检索过滤语义零变化
- 只重建 document 真的变了的条目（未变即跳过，省算力）

用法（容器内执行）
----------------
    docker cp scripts/reembed_requirement_vectors.py ai-test-gen-api:/app/_re.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _re.py            # dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _re.py --apply    # 重建
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

import asyncpg

from app.db.milvus_store import vector_store

DSN = os.environ.get(
    "REEMBED_DSN", "postgresql://aitestgen:aitestgen123@postgres:5432/ai_test_gen"
)
# 关注的内容块（用于报告"本次新增了什么"）
CFI_MARKERS = ("共享业务规则", "跨功能点流程", "功能点契约", "全局约束")


async def _load_requirements(include_archived: bool = False) -> dict[str, dict]:
    """从 PG 取需求（id → {srr_json, product_id, project_id, status}）。

    默认只取未归档需求（archived_at IS NULL）——归档需求的向量不在检索面内；
    `--include-archived` 时一并纳入（历史归档若仍在 Milvus 里可一并刷新）。
    """
    conn = await asyncpg.connect(DSN)
    where = "srr_json IS NOT NULL" if include_archived else "srr_json IS NOT NULL AND archived_at IS NULL"
    try:
        rows = await conn.fetch(
            f"""
            SELECT id, srr_json::text AS srr_text, product_id, project_id, status
            FROM requirements
            WHERE {where}
            """
        )
    finally:
        await conn.close()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            srr = json.loads(r["srr_text"])
        except Exception:
            continue
        out[r["id"]] = {
            "srr": srr,
            "product_id": r["product_id"] or "",
            "project_id": r["project_id"] or "",
            "status": r["status"] or "draft",
        }
    return out


def _existing_vectors() -> dict[str, dict]:
    """取 Milvus requirements 集合现有条目（id → {metadata, document}）。

    Milvus query 的 (offset+limit) 上限为 16384，超出直接报 code=65535。
    """
    out: dict[str, dict] = {}
    for row in vector_store.get_all_requirements(limit=16384):
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        out[row.get("id", "")] = {"metadata": meta, "document": row.get("document", "") or ""}
    if len(out) >= 16384:
        print("[warn] Milvus 条目达到 query 窗口上限 16384，结果可能不完整（需分页扩展脚本）")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="存量需求向量重建（T4 配套）")
    ap.add_argument("--apply", action="store_true", help="真正写库；不传则只报告")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全部，便于小样本验证）")
    ap.add_argument("--include-archived", action="store_true", help="一并刷新已归档需求的向量")
    args = ap.parse_args()

    pg = await _load_requirements(include_archived=args.include_archived)
    vec = _existing_vectors()
    print(f"PG 有效需求: {len(pg)} 条；Milvus requirements 条目: {len(vec)} 条")

    targets = [rid for rid, m in vec.items() if (m.get("metadata") or {}).get("doc_type", "requirement") == "requirement"]
    if args.limit:
        targets = targets[: args.limit]

    changed: list[tuple[str, int, int, list[str]]] = []
    missing_srr = 0
    unchanged = 0
    errors = 0

    for rid in targets:
        rec = pg.get(rid)
        if not rec:
            missing_srr += 1
            continue
        old_doc = vec[rid]["document"]
        new_doc = vector_store._srr_to_text(rec["srr"])
        if new_doc == old_doc:
            unchanged += 1
            continue
        gained = [kw for kw in CFI_MARKERS if kw in new_doc and kw not in old_doc]
        changed.append((rid, len(old_doc), len(new_doc), gained))

        if args.apply:
            meta = dict(vec[rid]["metadata"] or {})
            meta.setdefault("product_id", rec["product_id"])
            meta.setdefault("project_id", rec["project_id"])
            meta.setdefault("status", rec["status"])
            try:
                vector_store.add_requirement(rid, rec["srr"], metadata=meta)
            except Exception as e:  # noqa: BLE001 - 单条失败不阻断整批
                errors += 1
                print(f"  [ERR] {rid}: {repr(e)[:180]}")

    print("-" * 72)
    print(f"需重建（document 变化）: {len(changed)} 条")
    print(f"无需变更: {unchanged} 条；Milvus 有但 PG 无（跳过）: {missing_srr} 条；写入失败: {errors} 条")
    for rid, old_len, new_len, gained in changed[:40]:
        mark = "，新增: " + "/".join(gained) if gained else ""
        print(f"  · {rid}: {old_len} → {new_len} 字符{mark}")
    if len(changed) > 40:
        print(f"  … 其余 {len(changed) - 40} 条略")
    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行重建。")
    else:
        print(f"\n[apply] 已重建 {len(changed) - errors} 条向量。")


if __name__ == "__main__":
    asyncio.run(main())
