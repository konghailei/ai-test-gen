#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检索阈值标定工具（docs/14 一期配套）。

用途
----
为 `app.core.constants.RetrievalThreshold` 的两个相似度阈值提供**可复现**的
数据依据，替代"照搬业界 0.75"。docs/14 §6 明确要求：阈值不要照搬，
必须先用本项目真实数据看 score 分布再定。

原理
----
用**与生产完全一致的 query 构造**（`" / ".join(modules) + " " + fp_name`）
做两种情形对照，取分界带定阈值：

    S 信号  本产品内检索该 FP → 期望高分
    N 无关  同一 query 去检索**另一个产品** → 期望低分

阈值落在 `max(N)` 与 `min(S)` 之间即为有区分度的取值。

⚠️ 标定教训（勿重蹈）
------------------
必须用生产真实 query 构造。若图省事用 `requirement.title` 当 query，
信号区间只有 0.485~0.531、噪声 0.280~0.402，会得出"0.45 已足够"的
**错误结论**；生产 query 因含模块名与功能点名，信息量更大，相似度整体
上移约 0.10，阈值需相应提高。

用法
----
容器内执行（backend 已挂载到 /app，脚本在 /app/../scripts 不可见，故用 -v 或
直接在容器内以绝对路径调用）：

    # 方式一：从宿主机传入（推荐）
    docker cp scripts/calibrate_retrieval_threshold.py ai-test-gen-api:/app/_calib.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _calib.py

    # 方式二：若已把 scripts/ 挂进容器
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python /scripts/calibrate_retrieval_threshold.py

输出：两种情形的 min/p10/p25/中位/p75/max、建议阈值、以及阈值敏感性表
（每个候选阈值保留多少信号、误放多少无关）。

依赖：Milvus 中有已 approved 用例向量（否则无候选可评）。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from collections import Counter

from sqlalchemy import select

from app.db.database import async_session
from app.db.milvus_store import vector_store
from app.db.models import FunctionalPoint, Requirement, TestCase

# 候选阈值扫描点（按本项目实测 0.34~0.59 窄带设定）
SCAN_POINTS = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75)


def _pct(sorted_vals: list[float], p: float) -> float:
    k = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


def _dist(vals: list[float]) -> str:
    if not vals:
        return "（无数据）"
    s = sorted(vals)
    return (
        f"n={len(vals)} min={min(vals):.4f} p10={_pct(s, .10):.4f} p25={_pct(s, .25):.4f} "
        f"中位={statistics.median(vals):.4f} p75={_pct(s, .75):.4f} max={max(vals):.4f}"
    )


def build_query(modules: list[str], fp_name: str, req_text: str) -> str:
    """与 GenAgent.generate 中的 query 构造保持完全一致（勿单独改动本函数）。

    生产实现（agents/gen_agent.py）：
        _mod_text = " / ".join(m for m in modules if isinstance(m, str) and m)
        _query_text = " ".join(x for x in [_mod_text, _fp_text or _req_text] if x)[:300]
    """
    mod_text = " / ".join(m for m in (modules or []) if isinstance(m, str) and m)
    fp_text = (fp_name or "").strip()
    return " ".join(x for x in [mod_text, fp_text or req_text] if x).strip()[:300]


async def _collect_samples(limit_reqs: int, max_fp_per_req: int) -> tuple[str, str, list[tuple[str, str, str]]]:
    """挑选信号产品 A（approved 用例最多）与对照产品 B（次多），构造样本 query。"""
    async with async_session() as s:
        rows = (await s.execute(select(TestCase.product_id, TestCase.status))).all()
        approved: Counter[str] = Counter()
        for pid, st in rows:
            if st == "approved" and pid:
                approved[pid] += 1
        ranked = [p for p, _ in approved.most_common(2)]
        if len(ranked) < 2:
            raise SystemExit("[calib] 需至少 2 个含 approved 用例向量的产品，当前不足，退出")
        pa, pb = ranked
        print(f"[calib] 产品A(信号)={pa}  approved={approved[pa]}")
        print(f"[calib] 产品B(对照)={pb}  approved={approved[pb]}")

        reqs = (
            await s.execute(
                select(Requirement.id, Requirement.srr_json)
                .where(Requirement.product_id == pa)
                .limit(limit_reqs)
            )
        ).all()

        samples: list[tuple[str, str, str]] = []
        for req_id, srr in reqs:
            fps = (
                await s.execute(
                    select(FunctionalPoint.name, FunctionalPoint.modules)
                    .where(FunctionalPoint.requirement_id == req_id)
                    .limit(max_fp_per_req)
                )
            ).all()
            srr = srr if isinstance(srr, dict) else {}
            ri = srr.get("requirement") or {}
            req_mods = [m for m in (ri.get("modules") or []) if isinstance(m, str)]
            req_text = str(ri.get("summary") or ri.get("title") or "")
            for fp_name, fp_modules in fps:
                use_mods = [m for m in (fp_modules or []) if isinstance(m, str)] or req_mods
                samples.append((req_id, build_query(use_mods, fp_name, req_text), fp_name or ""))

        if not samples:
            raise SystemExit("[calib] 产品 A 下无可用 FP 样本，退出")
        print(f"[calib] 样本 query 数={len(samples)}")
    return pa, pb, samples


async def _scores(query: str, product_id: str, top_k: int, collection: str) -> list[float]:
    fn = (
        vector_store.search_similar_cases
        if collection == "test_cases"
        else vector_store.search_patterns
    )
    if collection == "test_cases":
        hits = await asyncio.to_thread(fn, query, top_k, None, None, product_id, None, None)
    else:
        hits = await asyncio.to_thread(fn, query, top_k)
    return [float(h["similarity"]) for h in hits if isinstance(h.get("similarity"), (int, float))]


async def main() -> int:
    ap = argparse.ArgumentParser(description="检索阈值标定（docs/14 一期）")
    ap.add_argument("--reqs", type=int, default=6, help="取样需求数（默认 6）")
    ap.add_argument("--fp-per-req", type=int, default=2, help="每个需求取几个 FP（默认 2）")
    ap.add_argument("--top-k", type=int, default=5, help="每次检索取 top-k（默认 5）")
    ap.add_argument("--collection", default="test_cases", choices=("test_cases", "patterns"))
    args = ap.parse_args()

    vector_store._ensure_connected()
    pa, pb, samples = await _collect_samples(args.reqs, args.fp_per_req)

    sig: list[float] = []
    noise: list[float] = []
    print(f"\n===== S) 信号：本产品({pa}) 内检索 =====", flush=True)
    for req_id, q, fp_name in samples:
        sims = await _scores(q, pa, args.top_k, args.collection)
        sig.extend(sims)
        print(f"  fp={fp_name[:16]:<16} top{args.top_k}={[round(x, 3) for x in sims]}", flush=True)
    print(f"[S 信号] {_dist(sig)}", flush=True)

    print(f"\n===== N) 无关：同 query 检索对照产品({pb}) =====", flush=True)
    for req_id, q, fp_name in samples:
        sims = await _scores(q, pb, args.top_k, args.collection)
        noise.extend(sims)
        print(f"  fp={fp_name[:16]:<16} top{args.top_k}={[round(x, 3) for x in sims]}", flush=True)
    print(f"[N 无关] {_dist(noise)}", flush=True)

    print("\n===== 分界分析与阈值敏感性 =====", flush=True)
    if not sig or not noise:
        print("  数据不足，无法分析", flush=True)
        return 1
    s_sorted, n_sorted = sorted(sig), sorted(noise)
    print(f"  信号 min={min(sig):.4f}  p10={_pct(s_sorted, .10):.4f}", flush=True)
    print(f"  无关 max={max(noise):.4f}  p90={_pct(n_sorted, .90):.4f}", flush=True)
    print(f"  → 分界带 [{max(noise):.4f}, {min(sig):.4f}]，中点建议 ≈ {(max(noise) + min(sig)) / 2:.3f}", flush=True)
    print("\n  阈值   保留信号    误放无关", flush=True)
    for t in SCAN_POINTS:
        keep_s = sum(1 for x in sig if x >= t) / len(sig) * 100
        keep_n = sum(1 for x in noise if x >= t) / len(noise) * 100
        print(f"  {t:.2f}   {keep_s:6.1f}%   {keep_n:6.1f}%", flush=True)

    print(
        "\n提示：当前 constants.RetrievalThreshold 的取值应与上表对齐；"
        "调整后需同步更新该类 docstring 中的实测分布。",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
