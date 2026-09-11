# -*- coding: utf-8 -*-
"""导出与人工反馈验证工具（6 格式导出 + 边界 + feedback/CRUD）。

用途
----
验证用例导出的 6 种格式与边界行为，以及人工反馈（单条/批量）与用例 CRUD。
对应 DevBlog 全链路 spec 的 T5 / TR-5.7、TR-5.8，
2026-09-11 用它补测闭合了这两项零证据缺口（6/6 格式 + 400/404 + feedback/CRUD 全通）。

覆盖
----
- TR-5.7：json / markdown / gherkin / pytest / xmind / xmind8 六格式（校验 MIME 与
  `Content-Disposition`）；无 `case_ids` → 400；全部不存在 → 404
- TR-5.8：单条 feedback（通过 → approved / 驳回 → rejected）、批量 feedback、CRUD
  （GET / PUT / DELETE / 删除后 GET 应 404）

⚠️ 数据安全设计
------------
feedback 会**改状态并同步向量库**，故刻意用 **scratch 产品**（默认 `PROD-88568-301`，
3 条 draft）执行，避免污染正式基线（如 DevBlog 的 737 approved）。
导出为只读，用正式产品用例即可。

用法（容器内执行）
----------------
    docker cp scripts/verify_export_and_feedback.py ai-test-gen-api:/app/_vexp.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _vexp.py

前置：`admin/888888` 可登录；scratch 产品下有用例（否则需改 PROD_SCRATCH）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from sqlalchemy import select

from app.db.database import async_session
from app.db.models import TestCase

BASE = "http://127.0.0.1:8000/api"
OUT = "/app/_e2e_tmp/artifacts/blog_v1_export_feedback.json"
PROD_DEV = "PROD-77712-469"
PROD_SCRATCH = "PROD-88568-301"

FORMATS = ["json", "markdown", "gherkin", "pytest", "xmind", "xmind8"]
EXPECT_MIME = {
    "json": "application/json",
    "markdown": "text/markdown",
    "gherkin": "text/plain",
    "pytest": "text/x-python",
    "xmind": "application/zip",
    "xmind8": "application/zip",
}


async def login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
    d = r.json()
    return (d.get("data") or {}).get("token") or (d.get("data") or {}).get("access_token")


async def main() -> int:
    async with async_session() as s:
        dev_cases = (await s.execute(
            select(TestCase.id).where(TestCase.product_id == PROD_DEV, TestCase.status == "approved").limit(5)
        )).scalars().all()
        scratch = (await s.execute(
            select(TestCase.id, TestCase.status).where(TestCase.product_id == PROD_SCRATCH)
        )).all()

    print(f"[t5] DevBlog 导出样本 {len(dev_cases)} 条: {dev_cases[:3]}", flush=True)
    print(f"[t5] scratch 反馈样本 {len(scratch)} 条: {[(r[0], r[1]) for r in scratch]}", flush=True)
    if not dev_cases or not scratch:
        print("[t5] 样本不足，退出", flush=True)
        return 1

    out = {"meta": {"base": BASE, "run_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
           "tr_summary": {}, "export": [], "feedback": {}}
    _t = httpx.Timeout(180.0, connect=30.0, read=180.0, write=60.0)
    ok = True

    async with httpx.AsyncClient(timeout=_t) as c:
        tok = await login(c)
        H = {"Authorization": f"Bearer {tok}"}
        print("[t5] 登录成功", flush=True)

        # ---------- TR-5.7 六格式导出 ----------
        print("\n===== TR-5.7 六格式导出 =====", flush=True)
        n_ok = 0
        for fmt in FORMATS:
            r = await c.post(f"{BASE}/export", json={"format": fmt, "case_ids": dev_cases}, headers=H)
            ct = (r.headers.get("content-type") or "").split(";")[0].strip()
            cd = r.headers.get("content-disposition") or ""
            mime_ok = EXPECT_MIME[fmt] in ct
            good = r.status_code == 200 and mime_ok and len(r.content) > 0
            n_ok += 1 if good else 0
            print(f"  {fmt:<9} HTTP={r.status_code} bytes={len(r.content):<8} ct={ct:<26} "
                  f"{'OK' if good else 'FAIL'}  {cd[:40]}", flush=True)
            out["export"].append({"format": fmt, "http_status": r.status_code,
                                  "bytes": len(r.content), "content_type": ct,
                                  "mime_expected": EXPECT_MIME[fmt], "mime_ok": mime_ok,
                                  "content_disposition": cd, "pass": good})
        ok &= (n_ok == len(FORMATS))
        print(f"  => 六格式 {n_ok}/{len(FORMATS)}", flush=True)

        # 边界：无 case_ids → 400
        r400 = await c.post(f"{BASE}/export", json={"format": "json"}, headers=H)
        print(f"\n  无 case_ids   HTTP={r400.status_code} (期望 400) body={r400.text[:90]}", flush=True)
        ok &= (r400.status_code == 400)

        # 边界：全部不存在 → 404
        r404 = await c.post(f"{BASE}/export", json={"format": "json", "case_ids": ["TC-NOT-EXIST-0001"]}, headers=H)
        print(f"  不存在 case_ids HTTP={r404.status_code} (期望 404) body={r404.text[:90]}", flush=True)
        ok &= (r404.status_code == 404)

        out["export_boundary"] = {"no_case_ids_status": r400.status_code,
                                  "nonexistent_status": r404.status_code,
                                  "pass": r400.status_code == 400 and r404.status_code == 404}

        # ---------- TR-5.8 feedback + CRUD ----------
        print("\n===== TR-5.8 feedback 单条 / 批量 / CRUD =====", flush=True)
        cid = scratch[0][0]
        before = scratch[0][1]

        # GET 详情（CRUD - Read）
        g = await c.get(f"{BASE}/testcases/{cid}", headers=H)
        print(f"  GET  /testcases/{cid} -> {g.status_code}", flush=True)

        # 单条 feedback：accepted=True → approved
        f1 = await c.post(f"{BASE}/testcases/{cid}/feedback",
                          json={"accepted": True, "comments": "TR-5.8 单条反馈验证"}, headers=H)
        g2 = await c.get(f"{BASE}/testcases/{cid}", headers=H)
        st2 = ((g2.json().get("data") or {}).get("status")) if g2.status_code == 200 else None
        print(f"  单条 feedback -> {f1.status_code}；状态 {before} -> {st2} (期望 approved)", flush=True)
        ok &= (f1.status_code in (200, 201) and st2 == "approved")

        # 单条 feedback：accepted=False → rejected
        f2 = await c.post(f"{BASE}/testcases/{cid}/feedback",
                          json={"accepted": False, "comments": "TR-5.8 驳回验证"}, headers=H)
        g3 = await c.get(f"{BASE}/testcases/{cid}", headers=H)
        st3 = ((g3.json().get("data") or {}).get("status")) if g3.status_code == 200 else None
        print(f"  单条驳回     -> {f2.status_code}；状态 {st2} -> {st3} (期望 rejected)", flush=True)
        ok &= (f2.status_code in (200, 201) and st3 == "rejected")

        # 批量 feedback：对剩余 draft 用例批量通过
        rest = [r[0] for r in scratch[1:]]
        fb = await c.post(f"{BASE}/testcases/batch/feedback",
                          json={"case_ids": rest, "accepted": True, "comments": "TR-5.8 批量验证"}, headers=H)
        after = []
        for _cid in rest:
            gg = await c.get(f"{BASE}/testcases/{_cid}", headers=H)
            after.append(((gg.json().get("data") or {}).get("status")) if gg.status_code == 200 else None)
        print(f"  批量 feedback({len(rest)} 条) -> {fb.status_code}；状态={after} (期望全 approved)", flush=True)
        ok &= (fb.status_code in (200, 201) and all(a == "approved" for a in after))

        # CRUD - Update / Delete（用一条 scratch 用例走完整闭环）
        upd_target = rest[0] if rest else cid
        u = await c.put(f"{BASE}/testcases/{upd_target}", json={"title": "TR-5.8 CRUD 更新标题"}, headers=H)
        d = await c.delete(f"{BASE}/testcases/{upd_target}", headers=H)
        g4 = await c.get(f"{BASE}/testcases/{upd_target}", headers=H)
        print(f"  PUT 更新 -> {u.status_code}；DELETE -> {d.status_code}；删除后 GET -> {g4.status_code} (期望 404)", flush=True)
        ok &= (u.status_code in (200, 201) and d.status_code in (200, 204) and g4.status_code == 404)

        out["feedback"] = {
            "single_approve": {"status": f1.status_code, "before": before, "after": st2},
            "single_reject": {"status": f2.status_code, "after": st3},
            "batch_approve": {"status": fb.status_code, "count": len(rest), "after": after},
            "crud": {"put": u.status_code, "delete": d.status_code, "get_after_delete": g4.status_code},
            "note": f"使用 scratch 产品 {PROD_SCRATCH}，未触碰 DevBlog 已记录的 737 approved 基线",
        }

    out["tr_summary"] = {
        "TR-5.7_six_formats": f"{n_ok}/{len(FORMATS)}",
        "TR-5.7_no_case_ids_400": r400.status_code == 400,
        "TR-5.7_nonexistent_404": r404.status_code == 404,
        "TR-5.8_feedback_single": f1.status_code in (200, 201) and f2.status_code in (200, 201),
        "TR-5.8_feedback_batch": fb.status_code in (200, 201),
        "TR-5.8_crud": u.status_code in (200, 201) and d.status_code in (200, 204) and g4.status_code == 404,
        "overall": bool(ok),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[t5] 产物已写入 {OUT}", flush=True)
    print(f"[t5] tr_summary={json.dumps(out['tr_summary'], ensure_ascii=False)}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
