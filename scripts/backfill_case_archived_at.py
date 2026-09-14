# -*- coding: utf-8 -*-
"""回填用例固化标记（N1 治理 · 用例库与需求库口径对齐）。

背景
----
`test_cases.archived_at` 列早已存在但**从未写入**（实测 1704 条全为 NULL）。
v3.3 起归档流程会在第 11b 步把「本项目下已审核通过的用例」标记 `archived_at`，
KB 问答的用例检索也只取已固化用例。但**历史数据没有标记**，
若不回填，已归档项目（如 DevBlog V1.0）的用例会被全部过滤掉。

本脚本按同一口径回填：对**每个已归档项目**，把其需求下
`status='approved'` 且 `archived_at IS NULL` 的用例，
标记为该项目自身的 `archived_at`（保持"随项目归档固化"的语义一致）。

安全设计
--------
- **默认 dry-run**，只统计与列出，不写数据
- `--apply` 才写入；且**只写 NULL**（幂等，不覆盖已有固化时间）
- 不触碰未归档项目——它们的用例本就不应被固化

用法（容器内执行）
----------------
    docker cp scripts/backfill_case_archived_at.py ai-test-gen-api:/app/_bf.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _bf.py          # dry-run
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _bf.py --apply  # 写入
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from sqlalchemy import func, select, update

from app.db.database import async_session
from app.db.models import Project, Requirement, TestCase

OUT = "/app/_e2e_tmp/artifacts/case_archived_backfill.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入；不传则 dry-run")
    args = ap.parse_args()

    async with async_session() as s:
        projects = (await s.execute(
            select(Project.id, Project.name, Project.version, Project.archived_at)
            .where(Project.archived_at.is_not(None))
        )).all()

    print(f"已归档项目: {len(projects)} 个")
    details, total = [], 0

    async with async_session() as s:
        for pid, pname, pver, arch_at in projects:
            req_ids = (await s.execute(
                select(Requirement.id).where(Requirement.project_id == pid)
            )).scalars().all()
            if not req_ids:
                continue
            n = (await s.execute(
                select(func.count()).select_from(TestCase).where(
                    TestCase.req_id.in_(req_ids),
                    TestCase.status == "approved",
                    TestCase.archived_at.is_(None),
                )
            )).scalar() or 0
            total_all = (await s.execute(
                select(func.count()).select_from(TestCase).where(
                    TestCase.req_id.in_(req_ids)
                )
            )).scalar() or 0
            details.append({"project_id": pid, "name": pname, "version": pver,
                            "archived_at": str(arch_at), "requirements": len(req_ids),
                            "cases_total": total_all, "cases_to_mark": n})
            print(f"  {pid}  {str(pname)[:26]:28} v{pver}  用例 {total_all} 条 "
                  f"→ 待标记 {n} 条")
            total += n

            if args.apply and n:
                await s.execute(
                    update(TestCase)
                    .where(
                        TestCase.req_id.in_(req_ids),
                        TestCase.status == "approved",
                        TestCase.archived_at.is_(None),
                    )
                    .values(archived_at=arch_at)
                )
        if args.apply:
            await s.commit()

    print(f"\n合计待标记 {total} 条；模式={'APPLY（已写入）' if args.apply else 'DRY-RUN（未写入）'}")

    if args.apply:
        async with async_session() as s:
            left = (await s.execute(
                select(func.count()).select_from(TestCase).where(TestCase.archived_at.is_(None))
            )).scalar()
            marked = (await s.execute(
                select(func.count()).select_from(TestCase).where(TestCase.archived_at.is_not(None))
            )).scalar()
        print(f"回填后：已固化 {marked} 条 / 仍为 NULL {left} 条")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"mode": "apply" if args.apply else "dry-run",
                   "projects": details, "total_to_mark": total}, f,
                  ensure_ascii=False, indent=2)
    print(f"产物: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
