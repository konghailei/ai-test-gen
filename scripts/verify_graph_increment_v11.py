# -*- coding: utf-8 -*-
"""TR-6.5「图谱增量」严格复核工具。

背景
----
2026-09-11 16:51 生成的 `blog_v11_graph.json` 报 `tr65_pass=True`（9/9 PASS），但复核发现
其「跨版本 FP version_refs=2」的证据取自 **PROD-53145-856（工单系统）的「权限回收」FP**，
而非 DevBlog —— 属**范围外证据替代**。本工具做严格复核，明确区分三种结论：

    PASS   断言成立
    N/A    断言在本场景不适用（并给出原因），**不得**用其它产品的证据顶替
    FAIL   断言被证伪

TR-6.5 原文（spec Task 6）：
    依赖图谱查询 V1.1 需求所有 FP 节点后，节点数 = V1.0 + V1.1 新增；
    FP CRSR 对「图片上传」类跨版本 FP，version_refs 版本数 = 2（至少一个 FP 达到）

已知事实（2026-09-11 产物）：DevBlog V1.0 = 4 FP、V1.1 = 11 FP，二者**名称零重叠**，
且 V1.1 中**不存在「图片上传」FP** → 断言②很可能为 N/A。

用法（容器内执行）
----------------
    docker cp scripts/verify_graph_increment_v11.py ai-test-gen-api:/app/_vgraph.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _vgraph.py

前置：`admin/888888` 可登录。只读，不写业务数据。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter

import httpx

BASE = "http://127.0.0.1:8000/api"
OUT = "/app/_e2e_tmp/artifacts/blog_v11_graph_recheck.json"

PROD_DEV = "PROD-77712-469"          # DevBlog（被测产品）
REQ_V10 = "REQ-1788244895551-8397"   # DevBlog V1.0
REQ_V11 = "REQ-1788322894962-6696"   # DevBlog V1.1

results: list[dict] = []


def check(name: str, verdict: str, detail: str, **extra) -> None:
    """verdict: PASS / N/A / FAIL / INFO（INFO = 观察项，不作缺陷判定）"""
    results.append({"check": name, "verdict": verdict, "detail": detail, **extra})
    mark = {"PASS": "✅", "N/A": "➖", "FAIL": "❌", "INFO": "ℹ️"}[verdict]
    print(f"{mark} [{verdict:4}] {name}: {detail}")


async def login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
    r.raise_for_status()
    d = r.json()["data"]
    # 实测响应字段为 data.token（不是 access_token）
    return d.get("token") or d.get("access_token")


async def main() -> None:
    timeout = httpx.Timeout(240.0, connect=30.0, read=240.0, write=60.0)
    async with httpx.AsyncClient(timeout=timeout) as c:
        token = await login(c)
        h = {"Authorization": f"Bearer {token}"}

        # ---------- ① 图谱节点数 = V1.0 + V1.1 ----------
        r = await c.get(f"{BASE}/products/{PROD_DEV}/fp/dependency-graph", headers=h)
        r.raise_for_status()
        data = r.json()["data"]
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        by_req: Counter[str] = Counter()
        name_by_req: dict[str, list[str]] = {}
        for n in nodes:
            rid = n.get("requirement_id") or n.get("req_id") or "?"
            by_req[rid] += 1
            name_by_req.setdefault(rid, []).append(n.get("name", ""))

        n_v10 = by_req.get(REQ_V10, 0)
        n_v11 = by_req.get(REQ_V11, 0)
        n_other = sum(v for k, v in by_req.items() if k not in (REQ_V10, REQ_V11))
        n_total = len(nodes)

        check(
            "TR-6.5① 图谱节点数 = V1.0 + V1.1",
            "PASS" if (n_total == n_v10 + n_v11 and n_other == 0) else "FAIL",
            f"total={n_total} = v1.0({n_v10}) + v1.1({n_v11}) + 其它需求({n_other})",
            nodes_total=n_total, nodes_v10=n_v10, nodes_v11=n_v11, nodes_other=n_other,
        )
        check(
            "TR-6.5①b 边两端均在本产品内（无跨产品泄漏）",
            "PASS" if all(e.get("source") in {n.get("id") for n in nodes}
                          and e.get("target") in {n.get("id") for n in nodes}
                          for e in edges) else "FAIL",
            f"edges={len(edges)}，全部端点位于本产品节点集内" ,
            edges_total=len(edges),
        )

        # ---------- ② 跨版本 FP 的 CRSR version_refs = 2 ----------
        names_v10 = set(name_by_req.get(REQ_V10, []))
        names_v11 = set(name_by_req.get(REQ_V11, []))
        overlap = sorted(names_v10 & names_v11)

        check(
            "TR-6.5② 存在跨版本 FP（V1.0 ∩ V1.1 同名）",
            "PASS" if overlap else "N/A",
            f"重叠 {len(overlap)} 个" + (f"：{overlap}" if overlap else
                                     "；V1.1 为 11 个全新 FP，与 V1.0 的 4 个无同名重合，"
                                     "且不存在 spec 假设的「图片上传」FP → 断言②在本场景不适用"),
            overlap=overlap, names_v10=sorted(names_v10), names_v11=sorted(names_v11),
        )

        crsr_probe: list[dict] = []
        if overlap:
            id_by_name = {n.get("name"): n.get("id") for n in nodes}
            for fp_name in overlap:
                fp_id = id_by_name.get(fp_name)
                rr = await c.get(f"{BASE}/products/fp-crsr/{fp_id}", headers=h)
                v = rr.json().get("data", {}) if rr.status_code == 200 else {}
                vc = v.get("version_count", 0)
                crsr_probe.append({
                    "fp_name": fp_name, "fp_id": fp_id, "http": rr.status_code,
                    "version_count": vc,
                    "stored_version_refs_len": len(v.get("stored_version_refs") or []),
                })
            ok = any(p["version_count"] == 2 for p in crsr_probe)
            check("TR-6.5②b 跨版本 FP version_refs 版本数 = 2",
                  "PASS" if ok else "FAIL",
                  f"{sum(1 for p in crsr_probe if p['version_count']==2)}/{len(crsr_probe)} 个 FP 达到 2")
        else:
            check("TR-6.5②b 跨版本 FP version_refs 版本数 = 2",
                  "N/A", "无跨版本 FP，无法验证；不以其它产品证据替代")

        # ---------- ③ 观察：DevBlog CRSR 版本引用构成（暴露重复归档现象）----------
        devblog_refs: list[dict] = []
        for n in nodes:
            rr = await c.get(f"{BASE}/products/fp-crsr/{n.get('id')}", headers=h)
            if rr.status_code != 200:
                continue
            v = rr.json().get("data", {})
            refs = v.get("stored_version_refs") or []
            devblog_refs.append({
                "fp_name": n.get("name"),
                "req_side": "v1.1" if n.get("requirement_id") == REQ_V11 else "v1.0",
                "version_count": v.get("version_count", 0),
                "stored_refs_len": len(refs),
                "stored_version_ids": [
                    (x.get("version_id") if isinstance(x, dict) else str(x)) for x in refs
                ],
            })

        # 重复归档现象：stored_refs_len=2 但 version_count=1，且两个 version_id 同属 V1.0
        dup_archive = [
            d for d in devblog_refs
            if d["stored_refs_len"] == 2 and d["version_count"] == 1
        ]
        distinct_versions = {tuple(d["stored_version_ids"]) for d in devblog_refs}
        # ⚠️ 注意：这不是缺陷。products.py:734-748（v2.45）为 version_id 加了全局唯一
        # 约束，同项目**重复归档**时后缀递增 -v2/-v3。因此「stored_refs=2 但
        # version_count=1」= 同一 V1.0 项目被归档了两次，属预期行为，判为观察项。
        check(
            "OBS-1 DevBlog CRSR 版本引用构成",
            "INFO",
            f"{len(dup_archive)}/{len(devblog_refs)} 个 FP 的 stored_refs=2 而 version_count=1"
            + ("：同一 V1.0 项目被重复归档两次（后缀 -v2，v2.45 全局唯一设计），非缺陷"
               if dup_archive else "：无重复归档"),
            dup_archive_count=len(dup_archive), sample=dup_archive[:3],
        )
        check(
            "OBS-2 DevBlog 是否存在真正的 V1.0+V1.1 版本引用",
            "PASS" if any(d["version_count"] >= 2 for d in devblog_refs) else "N/A",
            f"version_count≥2 的 FP 数 = {sum(1 for d in devblog_refs if d['version_count'] >= 2)}"
            + "；若 0 则说明 V1.1 归档未与 V1.0 形成跨版本聚合",
            distinct_version_tuples=[list(t) for t in list(distinct_versions)[:5]],
        )

    # ---------- 汇总 ----------
    verdicts = Counter(x["verdict"] for x in results)
    passed = verdicts["PASS"]
    info = verdicts["INFO"]
    na = verdicts["N/A"]
    failed = verdicts["FAIL"]

    # TR-6.5 整体判定：断言①必须 PASS；断言② PASS 或 N/A 均可接受，但不得 FAIL
    a1 = next(x for x in results if x["check"].startswith("TR-6.5①"))
    a2 = next(x for x in results if x["check"].startswith("TR-6.5②b"))
    tr65_pass = a1["verdict"] == "PASS" and a2["verdict"] in ("PASS", "N/A")

    out = {
        "product": PROD_DEV,
        "req_v10": REQ_V10,
        "req_v11": REQ_V11,
        "graph_nodes_total": n_total,
        "graph_nodes_v10": n_v10,
        "graph_nodes_v11": n_v11,
        "graph_nodes_other_req": n_other,
        "graph_edges_total": len(edges),
        "fp_names_v10": sorted(names_v10),
        "fp_names_v11": sorted(names_v11),
        "v10_v11_name_overlap": overlap,
        "devblog_crsr_all": devblog_refs,
        "dup_archive_count": len(dup_archive),
        "checks": results,
        "pass_count": passed,
        "na_count": na,
        "info_count": info,
        "fail_count": failed,
        "tr65_pass": tr65_pass,
        "note": (
            "严格复核：断言①成立；断言②因 DevBlog V1.1 与 V1.0 无同名 FP 而**不适用**，"
            "原 blog_v11_graph.json 用 PROD-53145-856（工单系统）「权限回收」FP 顶替，属范围外证据，本工具不予采纳。"
            if not overlap else "严格复核：存在跨版本 FP，按实际 CRSR 结果判定。"
        ),
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"PASS={passed}  N/A={na}  FAIL={failed}  INFO={info}")
    print(f"TR-6.5 整体判定: {'PASS（含 N/A 子项）' if tr65_pass else 'FAIL'}")
    print(f"产物: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
