# -*- coding: utf-8 -*-
"""失败轨迹回流评估集（P3-6，依据《深入理解 AI Agent》6.8 最有价值闭环）。

把生产失败案例（analysis_tasks 中 failed/error 的任务）脱敏后导出到
backend/eval/failure_cases.jsonl，作为评估集新用例与回归测试来源，
使评估集成"活资产"。

用法（容器内）：
  python /app/scripts/reflux_failures.py [--limit 20] [--days 30]

去重：按 task_id 幂等，重复执行不会产生重复条目。
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval")
FAILURE_FILE = os.path.join(EVAL_DIR, "failure_cases.jsonl")


async def main():
    ap = argparse.ArgumentParser(description="失败轨迹回流评估集")
    ap.add_argument("--limit", type=int, default=20, help="最多回流的失败任务数")
    ap.add_argument("--days", type=int, default=30, help="回溯天数")
    args = ap.parse_args()

    os.makedirs(EVAL_DIR, exist_ok=True)
    existing = set()
    if os.path.exists(FAILURE_FILE):
        with open(FAILURE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing.add(json.loads(line).get("task_id"))
                    except Exception:
                        pass

    from sqlalchemy import select
    from app.db.database import async_session
    from app.db.models import AnalysisTask, Requirement

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    async with async_session() as session:
        rows = (await session.execute(
            select(AnalysisTask)
            .where(AnalysisTask.status.in_(["failed", "error"]),
                   AnalysisTask.created_at >= since)
            .order_by(AnalysisTask.created_at.desc())
            .limit(args.limit)
        )).scalars().all()

    added = 0
    with open(FAILURE_FILE, "a", encoding="utf-8") as f:
        for task in rows:
            if task.id in existing:
                continue
            req = (await session.execute(
                select(Requirement).where(Requirement.id == task.req_id)
            )).scalar_one_or_none()
            rec = {
                "task_id": task.id,
                "req_id": task.req_id,
                "phase": task.phase,
                "status": task.status,
                "error": str(task.message or "")[:500],
                "created_at": str(task.created_at) if task.created_at else None,
                "req_title": (req.title if req else ""),
                "raw_text_excerpt": ((req.raw_text or "")[:2000] if req else ""),
                "srr_json": (req.srr_json if req else None),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            added += 1

    print(f"回流完成: 新增 {added} 条失败用例 → {FAILURE_FILE}（已有 {len(existing)} 条）")


if __name__ == "__main__":
    asyncio.run(main())
