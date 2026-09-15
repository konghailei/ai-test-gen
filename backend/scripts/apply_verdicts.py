# -*- coding: utf-8 -*-
"""把审阅台的判定落地成真实审核动作（HITL 的执行端，可复用）。

背景：审阅台产出的判定（`verdicts-auto.json` / 人工导出）原本只能看不能做，
需要人手点几十次。本脚本按判定分组执行：
    符合 → 批量 approve（PG 置 approved + 写 Milvus，建立回归基线）
    不符 → 批量 reject（备注写清与谁重复）
    存疑 → **不动**（保持 reviewed，等人工确认）
并保留执行前的状态快照，便于回滚核对。

用法（容器内）：
    # 先干跑看清影响面（默认就是 dry-run）
    docker exec ai-test-gen-api python /app/scripts/apply_verdicts.py \
        --run-dir /app/_review_runs/20260915-065020

    # 确认后真正执行
    ... --execute

    # 只处理某一类
    ... --execute --only ok
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COMMENTS = {
    "ok": "AI 判定：符合预期（合同完整、场景唯一、覆盖标注有效）→ 采纳入库。逐条依据见同目录 verdicts-auto.json",
    "bad": "AI 判定：与同批另一条场景重复（本批仅保留更完整的一条）→ 驳回，避免用例库冗余。逐条对照见 same-batch-duplicate 记录",
}
STATUS_LABEL = {"ok": "approved", "bad": "rejected"}


def load_verdicts(run_dir: str, path: str = "") -> dict:
    src = path or os.path.join(run_dir, "verdicts-auto.json")
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items") or {}


async def snapshot_statuses(case_ids: list[str]) -> dict:
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import TestCase

    async with async_session() as session:
        rows = (await session.execute(
            select(TestCase).where(TestCase.id.in_(case_ids))
        )).scalars().all()
        return {r.id: {"status": r.status, "confidence": r.confidence,
                       "feedback": r.feedback, "archived_at": r.archived_at.isoformat() if r.archived_at else None}
                for r in rows}


async def status_distribution(case_ids: list[str]) -> dict:
    snap = await snapshot_statuses(case_ids)
    dist: dict[str, int] = {}
    for v in snap.values():
        dist[v["status"]] = dist.get(v["status"], 0) + 1
    return dist


async def main() -> int:
    ap = argparse.ArgumentParser(description="按审阅判定执行批量审核")
    ap.add_argument("--run-dir", required=True, help="审阅运行目录（含 cases.json / verdicts-auto.json）")
    ap.add_argument("--verdicts", default="", help="判定文件路径（默认 <run-dir>/verdicts-auto.json）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="888888")
    ap.add_argument("--only", default="", choices=["", "ok", "bad", "doubt"], help="只处理某一类")
    ap.add_argument("--execute", action="store_true", help="真正执行（默认只干跑）")
    args = ap.parse_args()

    items = load_verdicts(args.run_dir, args.verdicts)
    cases = json.load(open(os.path.join(args.run_dir, "cases.json"), encoding="utf-8"))
    case_ids = [c["id"] for c in cases]

    groups: dict[str, list[str]] = {"ok": [], "bad": [], "doubt": []}
    for cid in case_ids:
        v = (items.get(cid) or {}).get("verdict")
        if v in groups:
            groups[v].append(cid)
    if args.only:
        for k in list(groups):
            if k != args.only:
                groups[k] = []

    existing = set((await snapshot_statuses(case_ids)).keys())
    print("=" * 74)
    print(f"判定分布: 符合 {len(groups['ok'])} / 不符 {len(groups['bad'])} / 存疑 {len(groups['doubt'])}"
          f"（用例总数 {len(case_ids)}，库中存在 {len(existing)}）")
    print(f"执行前状态分布: {await status_distribution(case_ids)}")
    print("=" * 74)

    plan = []
    for verdict in ("ok", "bad"):
        act_ids = [i for i in groups[verdict] if i in existing]
        missing = [i for i in groups[verdict] if i not in existing]
        if act_ids or missing:
            plan.append({"verdict": verdict, "action": STATUS_LABEL[verdict],
                         "count": len(act_ids), "ids": act_ids, "not_in_db": missing,
                         "comment": COMMENTS[verdict]})
    for p in plan:
        print(f"[{p['verdict']}] → {p['action']}: {p['count']} 条"
              + (f"，{len(p['not_in_db'])} 条不在库中（未落库）跳过" if p["not_in_db"] else ""))
    if groups["doubt"]:
        print(f"[doubt] → 不动作（保持 reviewed，等人工确认）: {len(groups['doubt'])} 条")

    if not args.execute:
        print("\n（干跑模式）加 --execute 才会真正写库。")
        return 0

    # 备份执行前状态（可回滚核对）
    backup_path = os.path.join(args.run_dir, "status-before-apply.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(await snapshot_statuses(case_ids), f, ensure_ascii=False, indent=2)
    print(f"\n执行前状态已备份: {backup_path}")

    import httpx

    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=30.0, read=240.0, write=60.0)) as c:
        r = await c.post(f"{args.base_url}/api/auth/login",
                         json={"username": args.username, "password": args.password})
        r.raise_for_status()
        data = r.json().get("data") or {}
        token = data.get("token") or data.get("access_token")
        headers = {"Authorization": f"Bearer {token}"}
        for p in plan:
            if not p["ids"]:
                continue
            resp = await c.post(
                f"{args.base_url}/api/testcases/batch/feedback",
                json={"case_ids": p["ids"], "accepted": p["verdict"] == "ok", "comments": p["comment"]},
                headers=headers,
            )
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            results.append({"verdict": p["verdict"], "http": resp.status_code,
                            "count": p["count"], "response": body.get("data") if isinstance(body, dict) else None,
                            "message": body.get("message") if isinstance(body, dict) else resp.text[:200]})
            print(f"[{p['verdict']}] HTTP {resp.status_code}: {results[-1]['message']}")

    after = await status_distribution(case_ids)
    print(f"\n执行后状态分布: {after}")

    out = {
        "executed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "run_dir": args.run_dir,
        "groups": {"ok": len(groups["ok"]), "bad": len(groups["bad"]), "doubt": len(groups["doubt"])},
        "actions": results,
        "status_before": json.load(open(backup_path, encoding="utf-8")),
        "status_after": await snapshot_statuses(case_ids),
    }
    with open(os.path.join(args.run_dir, "verdicts-executed.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"执行记录: {os.path.join(args.run_dir, 'verdicts-executed.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
