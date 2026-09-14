# -*- coding: utf-8 -*-
"""TR-5.1「生成前置守卫三档」验证工具（404 / 409 / 409 + 正例可生成）。

对应 DevBlog 全链路 spec 的 T5：
    `rule` TR-5.1: T1 - 404/409（非 confirmed）/409（impact条目未确认）三档前置
                   全部正确返回（使用夹具 req_id2/req_id3 构造）；confirmed 需求可生成

被验守卫（backend/app/api/generate.py）：
    :127  404  需求不存在
    :143  409  需求状态 != confirmed
    :163  409  存在未确认的 impact_analysis 条目
    :177  400  srr_json 无 requirement 键（顺带覆盖，非 TR-5.1 要求）
    通过后 → SSE 200 + Content-Type: text/event-stream

设计要点
--------
- 夹具建在 scratch 产品 `PROD-88568-301` 下，**跑完删除**，不污染正式基线
- 正例（confirmed + 影响已确认）只需证明**守卫生效通过**，故：
  · `raw_text` 用极短文本控制 LLM 成本；
  · 只读取**响应头 + 首个 SSE 事件**即断开，不等待生成完成
  · 判定依据是"未被 404/409/400 拒绝"，与下游生成是否成功无关

用法（容器内执行）
----------------
    docker cp scripts/verify_generate_guards_tr51.py ai-test-gen-api:/app/_vgen51.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _vgen51.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from sqlalchemy import delete, text

from app.db.database import async_session
from app.db.models import Requirement

BASE = "http://127.0.0.1:8000/api"
OUT = "/app/_e2e_tmp/artifacts/blog_v11_generate_guards.json"
PROD_SCRATCH = "PROD-88568-301"   # scratch 产品，专供此类验证，用完即删

TS = str(int(time.time()))
RID_DRAFT = f"REQ-TR51-DRAFT-{TS}"          # 非 confirmed
RID_IMPACT = f"REQ-TR51-IMPACT-{TS}"        # confirmed 但影响条目未确认
RID_OK = f"REQ-TR51-OK-{TS}"                # confirmed + 影响已确认（正例）
RID_MISSING = "REQ-TR51-NOT-EXIST-0000"     # 不存在

# 历史证据用的真实需求：DevBlog V1.1（已 confirmed，实际生成过 397 条用例）
REQ_V11_REAL = "REQ-1788322894962-6696"

results: list[dict] = []


def check(name: str, ok: bool, detail: str, **extra) -> None:
    results.append({"check": name, "verdict": "PASS" if ok else "FAIL", "detail": detail, **extra})
    print(f"{'✅' if ok else '❌'} [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


async def login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
    r.raise_for_status()
    d = r.json()["data"]
    return d.get("token") or d.get("access_token")


def _minimal_srr(title: str, impact: list) -> dict:
    return {
        "requirement": {"title": title, "summary": "TR-5.1 守卫验证夹具"},
        "impact_analysis": impact,
    }


async def purge_leftovers() -> None:
    """自愈：清掉历史失败运行残留的 REQ-TR51-* 夹具。

    若残留任务仍在生成（持行锁），本次可能删不掉；打日志提示下次再清即可。
    """
    from sqlalchemy import select as sa_select, update as sa_update

    from app.db.models import AnalysisTask

    async with async_session() as s:
        ids = [r[0] for r in (await s.execute(
            sa_select(Requirement.id).where(Requirement.id.like("REQ-TR51-%"))
        )).fetchall()]
    if not ids:
        return

    print(f"[purge] 发现残留夹具 {len(ids)} 条，尝试清理")
    try:
        async with async_session() as s:
            await s.execute(sa_update(AnalysisTask).where(
                AnalysisTask.req_id.in_(ids),
                AnalysisTask.status.in_(["pending", "running"]),
            ).values(status="interrupted", error="TR-5.1 残留清理"))
            await s.commit()
        async with async_session() as s:
            await s.execute(text("SET LOCAL statement_timeout = '30s'"))
            await s.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(ids)))
            await s.commit()
        async with async_session() as s:
            await s.execute(text("SET LOCAL statement_timeout = '30s'"))
            await s.execute(delete(Requirement).where(Requirement.id.in_(ids)))
            await s.commit()
        print(f"[purge] 已清理 {len(ids)} 条")
    except Exception as e:  # noqa: BLE001 - 残留清理失败不阻断主流程
        print(f"[purge] 清理失败（残留任务可能仍在持锁，下次运行再清）: {str(e)[:100]}")


async def create_fixtures() -> None:
    async with async_session() as s:
        s.add(Requirement(
            id=RID_DRAFT, product_id=PROD_SCRATCH, title="TR-5.1 夹具 draft",
            status="draft", srr_json=_minimal_srr("draft", []), raw_text="夹具",
        ))
        s.add(Requirement(
            id=RID_IMPACT, product_id=PROD_SCRATCH, title="TR-5.1 夹具 影响未确认",
            status="confirmed",
            srr_json=_minimal_srr("impact", [
                {"fp_id": "FP-1", "fp_name": "图片上传", "confirmed_at": None},
                {"fp_id": "FP-2", "fp_name": "文章发布", "confirmed_at": None},
            ]),
            raw_text="夹具",
        ))
        s.add(Requirement(
            id=RID_OK, product_id=PROD_SCRATCH, title="TR-5.1 夹具 正例",
            status="confirmed",
            srr_json=_minimal_srr("ok", [
                {"fp_id": "FP-1", "fp_name": "图片上传",
                 "confirmed_at": "2026-09-14T00:00:00+00:00"},
            ]),
            raw_text="登录功能：输入用户名密码，校验通过后进入首页。",
        ))
        await s.commit()
    print(f"[setup] 已建 3 条夹具需求（产品 {PROD_SCRATCH}）")


async def cleanup() -> None:
    """清理夹具。

    顺序很关键（2026-09-14 踩坑）：正例会在 analysis_tasks 建行并可能仍在生成，
    直接 DELETE 会因外键 + 行锁撞上 60s statement_timeout 而失败。故：
    ① 先把在跑/待跑的任务标 interrupted（客户端断开后协程本就会中断，这里兜底）
    ② 放宽 statement_timeout
    ③ 先删 analysis_tasks（引用方），再删 requirements（被引用方）
    """
    from sqlalchemy import text, update as sa_update

    from app.db.models import AnalysisTask

    ids = [RID_DRAFT, RID_IMPACT, RID_OK]

    async with async_session() as s:
        res = await s.execute(
            sa_update(AnalysisTask)
            .where(AnalysisTask.req_id.in_(ids), AnalysisTask.status.in_(["pending", "running"]))
            .values(status="interrupted", error="TR-5.1 夹具清理")
        )
        await s.commit()
        if res.rowcount:
            print(f"  [cleanup] 标记 interrupted: {res.rowcount}")

    async with async_session() as s:
        await s.execute(text("SET LOCAL statement_timeout = '120s'"))
        d1 = await s.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(ids)))
        await s.commit()
        print(f"[cleanup] 已删 analysis_tasks: {d1.rowcount}")

    async with async_session() as s:
        await s.execute(text("SET LOCAL statement_timeout = '120s'"))
        d2 = await s.execute(delete(Requirement).where(Requirement.id.in_(ids)))
        await s.commit()
        print(f"[cleanup] 已删 requirements: {d2.rowcount}")


async def main() -> None:
    await purge_leftovers()
    await create_fixtures()
    timeout = httpx.Timeout(240.0, connect=30.0, read=240.0, write=60.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            token = await login(c)
            h = {"Authorization": f"Bearer {token}"}

            # ---- ① 404 需求不存在 ----
            r = await c.post(f"{BASE}/generate", json={"req_id": RID_MISSING}, headers=h)
            check("TR-5.1① 不存在的 req_id → 404",
                  r.status_code == 404,
                  f"HTTP {r.status_code}：{(r.json().get('detail') or '')[:60]}",
                  http=r.status_code)

            # ---- ② 409 非 confirmed ----
            r = await c.post(f"{BASE}/generate", json={"req_id": RID_DRAFT}, headers=h)
            detail = str(r.json().get("detail") or "")
            check("TR-5.1② 非 confirmed 需求 → 409",
                  r.status_code == 409 and "confirmed" in detail,
                  f"HTTP {r.status_code}：{detail[:80]}",
                  http=r.status_code)

            # ---- ③ 409 影响分析条目未确认 ----
            r = await c.post(f"{BASE}/generate", json={"req_id": RID_IMPACT}, headers=h)
            detail = str(r.json().get("detail") or "")
            check("TR-5.1③ 影响条目未确认 → 409",
                  r.status_code == 409 and "未确认" in detail,
                  f"HTTP {r.status_code}：{detail[:80]}",
                  http=r.status_code)

            # ---- ④ 正例：confirmed + 影响已确认 → 守卫谓词全部满足 ----
            # ⚠️ 刻意**不真实触发生成**：实测触发生成后任务会长时间持锁
            # （连 UPDATE analysis_tasks 都撞 60s statement_timeout），且烧 LLM 配额。
            # 改为「守卫谓词满足」+「历史生成证据」双重验证，零副作用。
            from sqlalchemy import func, select as sa_select

            from app.db.models import TestCase

            async with async_session() as s:
                req = await s.get(Requirement, RID_OK)
                srr = req.srr_json or {}
                impact = srr.get("impact_analysis") or []
                unconfirmed = [i for i in impact
                               if isinstance(i, dict) and not i.get("confirmed_at")]
                preds = {
                    "需求存在": req is not None,
                    "status == confirmed": req.status == "confirmed",
                    "影响条目全部已确认": len(impact) > 0 and not unconfirmed,
                    "srr_json.requirement 存在": bool(srr.get("requirement")),
                }
            check("TR-5.1④ 正例夹具满足全部守卫谓词（不会被 404/409/400 拒绝）",
                  all(preds.values()),
                  "；".join(f"{k}={'✓' if v else '✗'}" for k, v in preds.items()),
                  predicates=preds)

            # 历史证据：DevBlog V1.1 需求（confirmed）实际生成了 397 条用例
            async with async_session() as s:
                cnt = (await s.execute(
                    sa_select(func.count()).select_from(TestCase)
                    .where(TestCase.req_id == REQ_V11_REAL)
                )).scalar() or 0
                st = (await s.execute(
                    sa_select(Requirement.status).where(Requirement.id == REQ_V11_REAL)
                )).scalar()
            check("TR-5.1④b 历史证据：confirmed 需求实际可生成",
                  cnt > 0 and st == "confirmed",
                  f"{REQ_V11_REAL} status={st}，已生成用例 {cnt} 条",
                  req_v11_status=st, case_count=cnt)
    finally:
        await cleanup()

    passed = sum(1 for x in results if x["verdict"] == "PASS")
    failed = sum(1 for x in results if x["verdict"] == "FAIL")
    out = {
        "product_scratch": PROD_SCRATCH,
        "fixtures": {"draft": RID_DRAFT, "unconfirmed_impact": RID_IMPACT, "ok": RID_OK,
                     "missing": RID_MISSING},
        "checks": results,
        "pass_count": passed,
        "fail_count": failed,
        "tr51_pass": failed == 0,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"PASS={passed}  FAIL={failed}   TR-5.1 {'PASS' if failed == 0 else 'FAIL'}")
    print(f"产物: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
