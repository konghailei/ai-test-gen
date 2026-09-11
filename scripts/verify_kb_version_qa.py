# -*- coding: utf-8 -*-
"""知识库「版本演进正确性」验证工具（KB 问答时点核对）。

用途
----
验证**版本迭代归档后，KB 问答是否采纳最新版本的规则**（而非旧版本值）。
对应 DevBlog 全链路 spec 的 T6 / TR-6.4「B 时点 KB 问答」，
2026-09-11 用它补测闭合了该项零证据缺口（3/3 PASS，产物 `blog_qa_v11.json`）。

验证方法
--------
对目标产品发 N 个"只在该版本才有明确值"的问题，检查：
  ① 答案是否含期望值（正则命中）
  ② 是否残留旧版本值 / 其他产品特征词（防跨版本、跨产品污染）
  ③ sources 是否指向本产品、有无跨产品越界
  ④ 是否回答了但**依据层级偏弱**（如靠 AI 生成的用例而非需求文档，见下）

⚠️ 已知观察（2026-09-11）：本系统 KB 对业务规则的依据常来自 **AI 生成的测试用例**
而非需求文档，存在"LLM 产出 → 入库 → 被当作依据反哺 LLM"的循环权威风险；
若需求文档本身自相矛盾，答案会退化为"从用例推断"。用本工具时请一并查看
`answer_full` 里的「信息来源 / 信息局限性」段落。

用法（容器内执行）
----------------
    docker cp scripts/verify_kb_version_qa.py ai-test-gen-api:/app/_vqa.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _vqa.py

前置：目标产品已归档且 KB 有向量；`admin/888888` 可登录。
注意：首次登录含 PBKDF2 + IPv6 fallback，实测可达 40s+，故超时放宽到 240s。

SSE 格式（实测）：
  event: sources → data 为 JSON **数组** [{id,title,type,similarity,summary}, ...]
  event: chunk   → data 为 markdown 单行（非 JSON）
  event: done    → data 为 JSON 对象，含 answer_full / reference_sources（首选）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

import httpx

BASE = "http://127.0.0.1:8000/api"
OUT = "/app/_e2e_tmp/artifacts/blog_qa_v11.json"
PROD_DEV = "PROD-77712-469"
PROD_OTHER = "PROD-53145-856"

QUESTIONS = [
    ("B1 素材库配额", "DevBlog 图片素材库的个人总量配额是多少？"),
    ("B2 手动标记上限", "DevBlog 手动标记版本最多保留多少个？超出如何处理？"),
    ("B3 敏感词禁言", "DevBlog 命中敏感词几次会触发禁言？禁言多久？"),
]

# V1.1 明确的期望值
EXPECT = {
    "B1 素材库配额": [r"2\s*GB", r"总量配额"],
    "B2 手动标记上限": [r"50\s*个"],
    "B3 敏感词禁言": [r"3\s*次", r"24\s*小时"],
}
# 旧值/越界残留（工单系统特征词）
STALE = [r"48\s*小时", r"72\s*小时", r"5\s*次", r"1\s*分钟", r"锁定"]


def parse_sse(text: str) -> tuple[str, list[dict]]:
    """解析 SSE：返回 (答案全文, sources)。优先用 done.answer_full。"""
    chunks: list[str] = []
    sources: list[dict] = []
    done_answer: str | None = None
    cur_event = ""
    for raw_line in text.splitlines():
        if raw_line.startswith("event:"):
            cur_event = raw_line[6:].strip()
            continue
        if not raw_line.startswith("data:"):
            continue
        payload = raw_line[5:].strip()
        if not payload:
            continue
        if cur_event == "sources":
            try:
                arr = json.loads(payload)
                if isinstance(arr, list):
                    sources.extend([s for s in arr if isinstance(s, dict)])
            except Exception:
                pass
        elif cur_event == "chunk":
            chunks.append(payload)          # markdown 单行，非 JSON
        elif cur_event == "done":
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict):
                if obj.get("answer_full"):
                    done_answer = str(obj["answer_full"])
                rs = obj.get("reference_sources")
                if isinstance(rs, list):
                    sources.extend([s for s in rs if isinstance(s, dict)])
    answer = done_answer if done_answer else "\n".join(chunks)
    return answer, sources


async def login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
    d = r.json()
    tok = (d.get("data") or {}).get("token") or (d.get("data") or {}).get("access_token")
    if not tok:
        raise RuntimeError("login 无 token: " + json.dumps(d, ensure_ascii=False)[:200])
    return tok


async def ask(c: httpx.AsyncClient, headers: dict, q: str) -> dict:
    t0 = time.time()
    async with c.stream("POST", f"{BASE}/knowledge-qa/ask",
                        json={"question": q, "product_id": PROD_DEV},
                        headers=headers) as resp:
        if resp.status_code != 200:
            return {"http_status": resp.status_code, "answer": "", "sources": [], "dur_s": 0}
        buf = b""
        async for chunk in resp.aiter_bytes():
            buf += chunk
            if len(buf) > 6_000_000:
                break
    answer, sources = parse_sse(buf.decode("utf-8", "replace"))
    return {"http_status": 200, "answer": answer, "sources": sources,
            "dur_s": round(time.time() - t0, 1)}


async def main() -> int:
    out = {"meta": {"base": BASE, "product_dev": PROD_DEV,
                    "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "note": "补测 T6/TR-6.4 B 时点 KB 问答（原 spec 无 qa_v11 产物）"},
           "tr_summary": {}, "results": []}
    _timeout = httpx.Timeout(240.0, connect=30.0, read=240.0, write=60.0)
    async with httpx.AsyncClient(timeout=_timeout) as c:
        tok = await login(c)
        headers = {"Authorization": f"Bearer {tok}", "Accept": "text/event-stream"}
        print(f"[qa] 登录成功 token_len={len(tok)}", flush=True)

        for tag, q in QUESTIONS:
            r = await ask(c, headers, q)
            ans = r["answer"]
            hits = [p for p in EXPECT[tag] if re.search(p, ans)]
            miss = [p for p in EXPECT[tag] if not re.search(p, ans)]
            stale = [p for p in STALE if re.search(p, ans)]
            src_ids = sorted({str(s.get("id") or "") for s in r["sources"] if s.get("id")})
            src_types = sorted({str(s.get("type") or "") for s in r["sources"]})
            cross = [i for i in src_ids if PROD_OTHER in i]
            ok = (not miss) and (not stale)
            print(f"\n===== {tag} =====", flush=True)
            print(f"  Q: {q}", flush=True)
            print(f"  HTTP={r['http_status']} 耗时={r['dur_s']}s 答案长度={len(ans)}", flush=True)
            print(f"  期望命中={hits} 未命中={miss} 旧值残留={stale} => {'PASS' if ok else 'FAIL'}", flush=True)
            print(f"  sources={len(r['sources'])} 条 types={src_types}", flush=True)
            print(f"  source_ids={src_ids[:8]}", flush=True)
            print(f"  答案：{ans[:500]}", flush=True)
            out["results"].append({
                "tag": tag, "question": q, "http_status": r["http_status"], "dur_s": r["dur_s"],
                "answer_len": len(ans), "answer_full": ans,
                "expect_hit": hits, "expect_miss": miss, "stale_hit": stale,
                "sources_count": len(r["sources"]), "source_types": src_types,
                "source_ids_sample": src_ids[:12], "source_cross_product": cross,
                "pass": ok,
            })

        res = out["results"]
        out["tr_summary"] = {
            "TR-6.4_B1_2GB": bool(res and res[0]["pass"]),
            "TR-6.4_B2_50个": bool(len(res) > 1 and res[1]["pass"]),
            "TR-6.4_B3_3次24小时": bool(len(res) > 2 and res[2]["pass"]),
            "TR-6.4_no_stale_residue": all(not r["stale_hit"] for r in res),
            "TR-6.6_isolation_no_cross": all(not r["source_cross_product"] for r in res),
            "all_have_sources": all(r["sources_count"] > 0 for r in res),
            "overall": all(r["pass"] for r in res) if res else False,
        }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[qa] 产物已写入 {OUT}", flush=True)
    print(f"[qa] tr_summary={json.dumps(out['tr_summary'], ensure_ascii=False)}", flush=True)
    return 0 if out["tr_summary"]["overall"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
