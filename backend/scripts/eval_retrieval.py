# -*- coding: utf-8 -*-
"""检索质量评估（P3-7，依据《深入理解 AI Agent》3.2：检索失败率 = 1 − recall@k）。

评估集格式（backend/eval/retrieval_evalset.jsonl，每行一例）：
  {"query": "...", "product_id": "PROD-xxx", "expected_req_ids": ["REQ-..."]}
  expected_req_ids: 该问题理应命中的需求/知识条目 ID（人工标注）

用法（容器内）：
  python /app/scripts/eval_retrieval.py [--top-k 20]

输出每例的命中情况 + 汇总 recall@5/10/20 与 MRR。
检索失败率 = 1 − recall@20，是 KB 质量的核心管理指标。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVALSET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "retrieval_evalset.jsonl")


async def main():
    ap = argparse.ArgumentParser(description="KB 检索质量评估（recall@k + MRR）")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--evalset", default=EVALSET)
    args = ap.parse_args()

    cases = []
    with open(args.evalset, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if not cases:
        print("评估集为空")
        sys.exit(2)

    from app.db.hybrid_search import hybrid_searcher
    from app.db.database import async_session

    recalls = {5: [], 10: [], 20: []}
    mrrs = []
    print(f"{'query':<30} {'top命中':<8} {'recall@5':>8} {'recall@10':>9} {'recall@20':>9} {'MRR':>6}")
    async with async_session() as session:
        for case in cases:
            results = await hybrid_searcher.search_requirements(
                query=case["query"],
                product_id=case.get("product_id"),
                modules=None,
                top_k=args.top_k,
                status="archived",
                session=session,
            )
            got_ids = []
            for r in results:
                rid = (r.get("id") or r.get("metadata", {}).get("req_id") or "")
                # FP 向量的 id 形如 FP-REQ-xxx，归一到源需求
                if rid.startswith("FP-"):
                    rid = rid[3:]
                got_ids.append(rid)
            expected = set(case["expected_req_ids"])
            ranks = [i + 1 for i, rid in enumerate(got_ids) if rid in expected]
            rr = 1.0 / ranks[0] if ranks else 0.0
            mrrs.append(rr)
            row = f"recall@k"
            out = f"{case['query'][:28]:<30} {'Y' if ranks else 'N':<8} "
            for k in (5, 10, 20):
                hit = 1.0 if any(rid in expected for rid in got_ids[:k]) else 0.0
                recalls[k].append(hit)
                out += f"{hit:>8} " if k == 5 else f"{hit:>9} " if k == 10 else f"{hit:>9} "
            print(out + f"{rr:>6.3f}")

    print("\n===== 汇总 =====")
    for k in (5, 10, 20):
        if recalls[k]:
            print(f"recall@{k}: {sum(recalls[k])/len(recalls[k]):.3f}")
    if mrrs:
        print(f"MRR: {sum(mrrs)/len(mrrs):.3f}")
    if recalls[20]:
        fail_rate = 1 - sum(recalls[20]) / len(recalls[20])
        print(f"检索失败率 (1-recall@{args.top_k}): {fail_rate:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
