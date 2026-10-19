# -*- coding: utf-8 -*-
"""回填：把已归档需求的跨功能点共享业务规则逐条编译成独立向量（v2.48）。

背景：shared_rule 逐条编译层是 v2.48 新增，此前归档的需求只有"整块 CFI 混进
需求级向量"的形态（语义稀释 → 检索不到）。本脚本对存量已归档需求补发 shared_rule
向量，等价于"重新走一遍归档时的共享规则编译步骤"。

用法（容器内）：
    python scripts/backfill_shared_rules.py                    # 全部已归档需求
    python scripts/backfill_shared_rules.py --product PROD-XXX # 限定产品
    python scripts/backfill_shared_rules.py --req REQ-XXX      # 限定需求
    python scripts/backfill_shared_rules.py --dry-run          # 只统计不出箱
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.shared_rules import iter_shared_rules, reconcile_shared_rules  # noqa: E402
from app.db.database import async_session  # noqa: E402
from app.db.models import Requirement  # noqa: E402


async def main(product: str, req: str, dry_run: bool) -> int:
    stmt = select(Requirement).where(Requirement.status == "archived")
    if product:
        stmt = stmt.where(Requirement.product_id == product)
    if req:
        stmt = stmt.where(Requirement.id == req)

    total_req = total_rules = total_enq = total_del = total_fail = 0
    async with async_session() as session:
        rows = (await session.execute(stmt)).scalars().all()
        print(f"待处理已归档需求: {len(rows)} 条")

        # 按产品分组：对账以「产品下所有归档需求」为期望集合，
        # 这样既补齐缺失、也删除已消失规则的过期条目。
        by_product: dict[str, list] = {}
        for r in rows:
            by_product.setdefault(r.product_id or "", []).append(r)

        for pid, reqs in by_product.items():
            print(f"\n== 产品 {pid or '(空)'} ({len(reqs)} 个归档需求) ==")
            for r in reqs:
                n = len(iter_shared_rules(r.srr_json or {}))
                print(f"  - {r.id} | {r.title or ''} | 规则 {n} 条")
                total_req += 1
                total_rules += n
            if dry_run:
                continue
            st = await reconcile_shared_rules(
                session, product_id=pid, archived_requirements=reqs,
                version_id="backfill-2026-10-16",
            )
            total_enq += st.get("upserted", 0)
            total_del += st.get("deleted", 0)
            total_fail += st.get("failed", 0)
        if not dry_run:
            await session.commit()

    print("\n=== 汇总 ===")
    print(f"  需求: {total_req} 条")
    print(f"  抽到规则: {total_rules} 条")
    print(f"  出箱 upsert: {total_enq} 条")
    print(f"  出箱 delete(过期): {total_del} 条")
    print(f"  出箱失败: {total_fail} 条")
    if dry_run:
        print("  （dry-run，未出箱）")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="回填共享业务规则向量")
    ap.add_argument("--product", default="", help="限定产品 ID")
    ap.add_argument("--req", default="", help="限定需求 ID")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不出箱")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.product, a.req, a.dry_run)))
