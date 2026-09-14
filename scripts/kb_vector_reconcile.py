# -*- coding: utf-8 -*-
"""向量化状态对账 / 自愈（N5-c）——命令行入口。

核心逻辑已抽到 `app/core/kb_vector_reconcile.py`，与应用内周期任务
（`app/core/kb_maintenance.py`）**共用同一套实现**，避免两份代码漂移。

背景
----
N5 追查发现 `requirements.vectorization_status` 与 Milvus 实际向量可能不一致：
- `REQ-1788322894962-6696` 标 `completed`，Milvus 中却**没有任何向量**
- 另有 3 条长期 `pending`（后查明**向量其实都在**，只是状态未回写）

三个机制缺陷：N5-a 派发无终态保证 / N5-b 纠错删→重嵌非原子 / N5-c 无对账机制。

安全设计
--------
- **默认 dry-run**，只报告不写数据
- `--repair` 只做「补写缺失向量」与「修正状态」，**绝不删除**
- 补写时 metadata **保留真实 status**，不伪造成 archived
  （否则会绕过检索侧 status 过滤，等于偷偷改变检索语义）

用法（容器内执行）
----------------
    docker cp scripts/kb_vector_reconcile.py ai-test-gen-api:/app/_rec.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _rec.py            # dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _rec.py --repair   # 修复
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.core.kb_vector_reconcile import reconcile_requirements
from app.core.kb_maintenance import _run_once

OUT = "/app/_e2e_tmp/artifacts/kb_vector_reconcile.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", action="store_true", help="执行修复；不传则只报告")
    ap.add_argument("--once", action="store_true",
                    help="模拟周期任务跑一轮（含孤儿扫描，只报告）")
    args = ap.parse_args()

    if args.once:
        await _run_once()
        return

    rep = await reconcile_requirements(repair=args.repair)

    print(f"Milvus requirements 向量数: {rep['vector_count']}；"
          f"PG 需求数: {rep['pg_requirement_count']}")
    print("\n=== 对账结果 ===")
    print(f"  ✅ 标 completed 且真有向量: {rep['completed_with_vector']}")
    print(f"  ❌ 标 completed 但无向量（需补写）: {len(rep['completed_without_vector'])}")
    for m in rep["completed_without_vector"]:
        print(f"       {m['id']}  status={m['status']}  {str(m['title'])[:30]}")
    print(f"  ❌ 卡中间态且无向量（需补写）: {len(rep['stuck_and_missing'])}")
    for m in rep["stuck_and_missing"]:
        print(f"       {m['id']}  status={m['status']}  {str(m['title'])[:30]}")
    print(f"  ⚠️ 卡中间态但向量已在（仅改状态）: {len(rep['stuck_but_present'])}")
    for m in rep["stuck_but_present"]:
        print(f"       {m['id']}  status={m['status']}  {str(m['title'])[:30]}")

    if args.repair:
        print(f"\n  ✅ 补写向量: {rep['repaired']} 条")
        print(f"  ✅ 修正状态: {rep['status_only_fixed']} 条")
        for f in rep["repair_failed"]:
            print(f"  ❌ 失败 {f['id']}: {f['error'][:100]}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"mode": "repair" if args.repair else "dry-run", **rep},
                  f, ensure_ascii=False, indent=2)
    print(f"\n产物: {OUT}")
    if not args.repair and (rep["completed_without_vector"]
                            or rep["stuck_and_missing"]
                            or rep["stuck_but_present"]):
        print("确认清单后加 --repair 才修复（只新增/改状态，不删除）。")


if __name__ == "__main__":
    asyncio.run(main())
