# -*- coding: utf-8 -*-
"""Judge 金标校准（P2-4，依据《深入理解 AI Agent》6.4/6.5.1）。

LLM-as-Judge 大规模启用前，必须先建金标集并校准：
  Cohen's kappa > 0.7 才允许用于评审；judge 模型或 rubric 更新后须重校。

金标集格式（JSONL，每行一个用例级人工标注）：
  {"case_id": "TC-工单管理-8465-7952", "human_flagged": true,  "dimension": "deduplication"}
  {"case_id": "TC-工单管理-8465-7960", "human_flagged": false}
  - human_flagged: 人工判断该用例是否存在真实质量问题
  - dimension:     存在问题时的问题维度（可选，用于分维度 kappa）

模型侧标签：从指定评审报告的 issues 推导（case_id 出现在 issues 中 = 模型标记有问题）。

用法（容器内）：
  python /app/scripts/calibrate_judge.py --report-id RR-xxxx --golden /app/eval/golden/review_golden.jsonl
退出码：0 = kappa>0.7 可用；1 = kappa<=0.7 不可用；2 = 数据不足。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KAPPA_THRESHOLD = 0.7
MIN_SAMPLES = 30  # 书 6.4：金标集 100-200 条起步；低于 30 条结果无意义


async def main():
    ap = argparse.ArgumentParser(description="评审 Judge 金标校准（Cohen's kappa）")
    ap.add_argument("--report-id", required=True, help="评审报告 ID（模型侧标签来源）")
    ap.add_argument("--golden", required=True, help="金标 JSONL 路径")
    args = ap.parse_args()

    from app.core.eval_stats import cohens_kappa

    golden = {}
    with open(args.golden, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            golden[rec["case_id"]] = rec

    from app.db.database import async_session
    from sqlalchemy import select
    from app.db.models import ReviewReportRecord
    async with async_session() as session:
        row = (await session.execute(
            select(ReviewReportRecord).where(ReviewReportRecord.id == args.report_id)
        )).scalar_one_or_none()
    if row is None:
        print(f"报告不存在: {args.report_id}")
        sys.exit(2)

    model_flagged = set()
    for issue in (row.issues or []):
        cid = issue.get("case_id")
        if cid:
            model_flagged.add(cid)

    # 对齐样本：只取人工标注过的用例
    common = [cid for cid in golden if cid in {c.get("case_id") for c in (row.issues or [])} or True]
    # 严格口径：human 标注全集（模型"未标记"也是有效判定）
    common = list(golden.keys())
    if len(common) < MIN_SAMPLES:
        print(f"金标样本不足: {len(common)} < {MIN_SAMPLES}（书 6.4 建议 100-200 条起步）")
        sys.exit(2)

    human = [bool(golden[cid]["human_flagged"]) for cid in common]
    model = [cid in model_flagged for cid in common]

    both = sum(1 for hm, m in zip(human, model) if hm and m)
    print(f"样本数: {len(common)}")
    print(f"  人工标记有问题: {sum(human)}  模型标记有问题: {sum(model)}  双方一致标记: {both}")
    kappa = cohens_kappa(human, model)
    print(f"  Cohen's kappa = {kappa:.3f}（门槛 {KAPPA_THRESHOLD}）")

    # 分维度 kappa（可选）
    dims = {}
    for cid in common:
        dim = golden[cid].get("dimension")
        if dim:
            dims.setdefault(dim, ([], []))
            dims[dim][0].append(bool(golden[cid]["human_flagged"]))
            dims[dim][1].append(cid in model_flagged)
    for dim, (hm, md) in dims.items():
        if len(hm) >= 5:
            print(f"  [{dim}] kappa = {cohens_kappa(hm, md):.3f} (n={len(hm)})")

    if kappa > KAPPA_THRESHOLD:
        print("  ⇒ 校准通过，judge 可用于评审")
        sys.exit(0)
    else:
        print("  ⇒ 校准不通过：judge 输出与人工判断一致性不足，禁止用于大规模评审")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
