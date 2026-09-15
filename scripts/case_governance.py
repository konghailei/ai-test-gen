# -*- coding: utf-8 -*-
"""用例库治理扫描（docs/14 方向 B / B4）——命令行入口。

核心逻辑在 `app/core/case_governance.py`（与未来的周期任务共用，避免两份实现）。

## ⚠️ 当前会输出"空报告"，这是**预期**的

实测（2026-09-15，1704 条用例）三条判据全部命中 0 条：

| 判据 | 命中 | 原因 |
|---|---|---|
| `weight < 0.3` 且 `execution_count = 0` | 0 | approved 用例权重集中在 0.5 档，尚未分化 |
| 入库超 6 月且从未复用 | 0 | **全部用例入库不足 1 个月** |
| 相似度 ≥ 0.92 的近似重复 | 0 | 实测最高仅 0.890（同产品用例本就话题相近） |

工具本身是**能力就位**——等库龄与使用量积累后自然产出内容。
**不要因为报告为空就以为用例库健康**（脚本会打印 `empty_reason` 说明原因）。

## 用法（容器内执行）

    docker cp scripts/case_governance.py ai-test-gen-api:/app/_gov.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gov.py
    # 含近似重复检测（较慢，每条样本一次向量检索）：
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gov.py --dup
    # 指定产品与阈值：
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _gov.py --product PROD-77712-469 --dup-threshold 0.86
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from app.core.case_governance import (
    DUP_SIMILARITY,
    LOW_WEIGHT,
    STALE_MONTHS,
    scan_case_library,
)

OUT = "/app/_e2e_tmp/artifacts/case_governance_report.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default=None, help="按产品筛选")
    ap.add_argument("--dup", action="store_true", help="包含近似重复检测（较慢）")
    ap.add_argument("--dup-threshold", type=float, default=DUP_SIMILARITY)
    ap.add_argument("--low-weight", type=float, default=LOW_WEIGHT)
    ap.add_argument("--stale-months", type=int, default=STALE_MONTHS)
    args = ap.parse_args()

    report = await scan_case_library(
        product_id=args.product,
        low_weight=args.low_weight,
        stale_months=args.stale_months,
        dup_similarity=args.dup_threshold,
        include_dup_scan=args.dup,
    )

    print(f"用例总数: {report['total_cases']}  "
          f"库龄: {report['library_age']['earliest'][:10]} ~ "
          f"{report['library_age']['latest'][:10]}")

    print(f"\n① 低权重且从未复用（weight < {args.low_weight}）: "
          f"{report['low_weight']['count']} 条")
    for c in report["low_weight"]["cases"][:10]:
        print(f"     w={c['weight']}  {str(c['title'])[:50]}")

    print(f"\n② 长期未命中（超 {args.stale_months} 月且从未复用）: "
          f"{report['stale']['count']} 条")
    for c in report["stale"]["cases"][:10]:
        print(f"     {c['created_at'][:10]}  {str(c['title'])[:50]}")

    dup = report.get("duplicates", {})
    if dup.get("skipped"):
        print(f"\n③ 近似重复: 已跳过（加 --dup 启用，阈值 {dup.get('threshold')}）")
    else:
        st = dup.get("similarity_stats") or {}
        print(f"\n③ 近似重复（阈值 {dup.get('threshold')}）: {dup.get('pair_count')} 对"
              f"  [样本 {dup.get('sampled')}，比对 {dup.get('compared')}]")
        print(f"     相似度分布: min={st.get('min')} p50={st.get('p50')} max={st.get('max')}")
        for p in (dup.get("pairs") or [])[:10]:
            print(f"     {p['similarity']}  {str(p['title'])[:40]}")

    if report.get("empty_reason"):
        print("\n=== ⚠️ 为什么是空的 ===")
        for e in report["empty_reason"]:
            print(f"   - {e}")
    print(f"\n{report.get('_disclaimer', '')}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n产物: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
