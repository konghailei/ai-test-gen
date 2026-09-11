# -*- coding: utf-8 -*-
"""澄清回答来源审计验证工具（T2 / TR-2.2 / TR-2.6）。

用途
----
验证「上传 → 解析 → 澄清问答 → 提交 → 收敛」全链路，并产出**逐题来源审计**：
每题标注答案来源（`system_options` 系统选项 / `llm_from_original` 依原文生成），
**不得出现 `script_driven`**（脚本硬编码兜底）。

对应 DevBlog 全链路 spec 的 T2 / TR-2.2、TR-2.6。2026-09-11 首次补测时，
原 spec 的 `blog_v1_clarify.json` 显示 `all_answers_total=0`、`rounds_total=0`、
`force_passed=True`——即**从未真正提交过答案**，来源审计是空集，故本工具补齐该证据。

回答来源的既定口径（项目约定，见 TEST_PLAN §6.2.1 / v4.5）
--------------------------------------------------------
澄清回答**强制由模型生成严谨答案**（具体值/规则/边界行为），
**禁用脚本泛化兜底**（"按常规处理""遵循现有规则"这类无信息回答）。
本工具沿用该口径，agent_name 用 `E2E.AnswerGen` 以便日志归因：

  - 题目**带 options** → 让模型从选项中选出与原文一致的项 → `source=system_options`
  - 题目**无 options** → 让模型依据 PRD 原文写出具体规则 → `source=llm_from_original`
    （文档确实未定义时，按项目口径给出一个合理具体规则，并在 artifact 中标记
      `doc_gap=true`，作为"需求文档结构性缺失"信号）

用法（容器内执行）
----------------
    docker cp scripts/verify_clarify_answer_audit.py ai-test-gen-api:/app/_vca.py
    docker exec -e PYTHONPATH=/app -w /app ai-test-gen-api python _vca.py

默认目标：产品 `PROD-63410-827`（TEST_PLAN §20.2 标注为"同名产品，未启用"的空壳），
项目版本 `T2-AUDIT-1.0`（同一产品内版本号唯一，重跑请换版本号或清理旧项目）。

产出：`/app/_e2e_tmp/artifacts/blog_v1_clarify_audit.json`
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000/api"
DEFAULT_OUT = "/app/_e2e_tmp/artifacts/blog_v1_clarify_audit.json"
DEFAULT_PRD = "/app/tests/博客创作平台-产品需求文档PRD.md"
DEFAULT_PRODUCT = "PROD-63410-827"   # 空壳产品（未启用）
DEFAULT_VERSION = "T2-AUDIT-1.0"

ANSWER_SYSTEM = "你是一位资深需求分析师，正在替需求方回答澄清问题。只输出 JSON。"

ANSWER_PROMPT = """你是资深需求分析师，正在替需求方回答下列澄清问题（模拟一位会认真完整作答的理想用户）。

【需求文档原文（节选）】
{prd}

【待回答问题】
{items}

回答要求：
1. 每个问题必须覆盖**全部子问点**（如"是否有重试上限、备用方案、提示文案"要逐一明确），不得遗漏。
2. 答案必须给出**具体数值/规则/行为/文案**（次数、时长、格式、范围、提示语），
   禁止笼统表述（"按常规处理""遵循现有规则"）。
3. **来源判定（关键）**：
   - 若该题带 `options`（选项列表）：从 options 中选出与需求文档一致的一项作为答案，
     `source` 填 `"system_options"`。
   - 若该题无 options：依据需求文档原文写出具体规则，`source` 填 `"llm_from_original"`。
     若文档确实未定义该点，给出一条合理的具体规则，并把 `doc_gap` 填 `true`（否则 false）。
4. 每个答案 40-150 字，中文，需求方确认口吻。

输出 JSON：
{{"answers": [{{"id": "原id", "answer": "答案", "source": "system_options|llm_from_original", "doc_gap": false}}, ...]}}
顺序与输入一致，只输出 JSON。
"""


async def login(c: httpx.AsyncClient) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
    d = r.json()
    tok = (d.get("data") or {}).get("token") or (d.get("data") or {}).get("access_token")
    if not tok:
        raise RuntimeError("login 无 token: " + json.dumps(d, ensure_ascii=False)[:200])
    return tok


async def poll_task(c: httpx.AsyncClient, H: dict, task_id: str, timeout_s: int = 900) -> dict:
    """轮询任务到终态，返回 task dict。"""
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout_s:
        r = await c.get(f"{BASE}/requirements/tasks/{task_id}", headers=H)
        d = (r.json().get("data") or {}) if r.status_code == 200 else {}
        st, ph, pg = d.get("status"), d.get("phase"), d.get("progress")
        line = f"{st}/{ph}/{pg}"
        if line != last:
            print(f"      [task] {line} {str(d.get('message') or '')[:50]}", flush=True)
            last = line
        if st in ("completed", "failed", "interrupted", "cancelled", "done") or ph == "done":
            return d
        await asyncio.sleep(4)
    print(f"      [task] 轮询超时 {timeout_s}s", flush=True)
    return {"status": "timeout", "task_id": task_id}


async def gen_answers(questions: list[dict], prd: str) -> dict[str, dict]:
    """用后端已配置的 LLM 生成严谨答案（沿用 E2E.AnswerGen 口径）。"""
    from app.core.llm_client import llm_client

    items = []
    for q in questions:
        opts = q.get("options") or []
        items.append({
            "id": q.get("id", ""),
            "question": str(q.get("question", ""))[:600],
            "options": opts if isinstance(opts, list) else [],
            "reason": str(q.get("_followup_reason") or "")[:200],
        })
    prompt = ANSWER_PROMPT.format(prd=prd[:14000], items=json.dumps(items, ensure_ascii=False))
    result = await llm_client.call_json(
        prompt=prompt, system_prompt=ANSWER_SYSTEM,
        schema_hint={"answers": list}, temperature=0.2, agent_name="E2E.AnswerGen",
    )
    out: dict[str, dict] = {}
    for a in (result.get("answers") or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        out[str(a["id"])] = {
            "answer": str(a.get("answer", "") or ""),
            "source": str(a.get("source") or ""),
            "doc_gap": bool(a.get("doc_gap")),
        }
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="澄清回答来源审计（T2 / TR-2.2 / TR-2.6）")
    ap.add_argument("--product-id", default=DEFAULT_PRODUCT)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--prd", default=DEFAULT_PRD)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--project-id", default="", help="复用已有项目（留空则新建）")
    ap.add_argument("--req-id", default="",
                    help="复用已有需求（跳过上传/解析，直接从澄清轮开始）。"
                         "用于解析阶段轮询超时后的续跑——免费供应商单次 LLM 调用可达 8 分钟+")
    args = ap.parse_args()

    if not args.req_id and not os.path.exists(args.prd):
        print(f"[t2] PRD 不存在: {args.prd}", flush=True)
        return 2
    prd = open(args.prd, encoding="utf-8").read() if os.path.exists(args.prd) else ""
    print(f"[t2] PRD 读取完成: {len(prd)} 字", flush=True)

    # 装载 DB 中的 LLM 供应商配置（独立进程不走 lifespan）
    from app.db.database import async_session
    from app.api.settings import load_db_config_to_settings
    async with async_session() as s:
        await load_db_config_to_settings(s)
    print("[t2] LLM 配置已装载", flush=True)

    # 续跑模式：优先用需求自带的 _raw_text 作回答上下文（比外部 PRD 更贴合）
    if args.req_id:
        from sqlalchemy import select as _sel
        from app.db.models import Requirement as _Req
        async with async_session() as s:
            row = (await s.execute(
                _sel(_Req.srr_json, _Req.title, _Req.product_id).where(_Req.id == args.req_id)
            )).first()
        if not row:
            print(f"[t2] 需求不存在: {args.req_id}", flush=True)
            return 2
        _srr, _title, _pid = row
        _raw = (_srr or {}).get("_raw_text") or ""
        if _raw:
            prd = _raw
            print(f"[t2] 续跑模式：使用需求自带 _raw_text 作上下文（{len(prd)} 字）", flush=True)
        else:
            print(f"[t2] 续跑模式：需求无 _raw_text，沿用 --prd（{len(prd)} 字）", flush=True)
        if not args.product_id or args.product_id == DEFAULT_PRODUCT:
            args.product_id = _pid or args.product_id
        print(f"[t2] 目标需求={args.req_id} 标题={( _title or '')[:40]} 产品={args.product_id}", flush=True)

    out = {"meta": {"base": BASE, "product_id": args.product_id, "version": args.version,
                    "prd_len": len(prd), "run_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
           "rounds": [], "tr_summary": {}}

    _t = httpx.Timeout(300.0, connect=30.0, read=300.0, write=120.0)
    async with httpx.AsyncClient(timeout=_t) as c:
        tok = await login(c)
        H = {"Authorization": f"Bearer {tok}"}
        print("[t2] 登录成功", flush=True)

        # 1) 项目
        proj_id = args.project_id
        if not proj_id and not args.req_id:
            r = await c.post(f"{BASE}/products/{args.product_id}/projects",
                             json={"name": f"T2 澄清来源审计 {args.version}",
                                   "description": "TR-2.2/2.6 补测专用项目",
                                   "version": args.version}, headers=H)
            print(f"[t2] 创建项目 HTTP={r.status_code} {r.text[:160]}", flush=True)
            if r.status_code not in (200, 201):
                print("[t2] 创建项目失败，退出", flush=True)
                return 1
            proj_id = ((r.json().get("data") or {}).get("id")
                       or (r.json().get("data") or {}).get("project_id"))
        print(f"[t2] 项目={proj_id}", flush=True)
        out["meta"]["project_id"] = proj_id

        # 2) 上传（--req-id 时跳过，直接续跑到澄清轮）
        if args.req_id:
            req_id = args.req_id
            print(f"[t2] 续跑模式：直接使用已有需求 {req_id}", flush=True)
            out["meta"]["req_id"] = req_id
            out["meta"]["parse_final"] = {"status": "skipped(resume)"}
            t = {"status": "skipped"}
        else:
            r = await c.post(f"{BASE}/requirements/upload",
                             json={"raw_text": prd, "product_id": args.product_id,
                                   "project_id": proj_id,
                                   "title": "DevBlog 技术博客创作平台（T2 审计）", "priority": "P2"},
                             headers=H)
            print(f"[t2] 上传 HTTP={r.status_code} {r.text[:200]}", flush=True)
            if r.status_code not in (200, 201):
                return 1
            d0 = r.json().get("data") or {}
            task_id, req_id = d0.get("task_id"), d0.get("req_id") or d0.get("id")
            print(f"[t2] task={task_id} req={req_id}", flush=True)
            out["meta"].update({"task_id": task_id, "req_id": req_id})

            # 3) 等解析（免费供应商单次调用可达 8 分钟+，故超时给到 25 分钟）
            print("[t2] 等待解析完成...", flush=True)
            t = await poll_task(c, H, task_id, timeout_s=1500)
            out["meta"]["parse_final"] = {"status": t.get("status"), "phase": t.get("phase")}
            if t.get("status") not in ("completed", "done"):
                print(f"[t2] 解析未在超时内完成: {t.get('status')}。"
                      f"可用 --req-id {req_id} 续跑澄清轮", flush=True)
                _write(args.out, out)
                return 1

        # 4) 澄清多轮
        converged_round = None
        all_answers: dict[str, dict] = {}
        for rnd in range(1, args.max_rounds + 1):
            g = await c.get(f"{BASE}/requirements/{req_id}/clarify/questions", headers=H)
            qd = (g.json().get("data") or {}) if g.status_code == 200 else {}
            qs = qd.get("questions") or []
            print(f"\n[t2] === 第 {rnd} 轮：待答问题 {len(qs)} 个（status={qd.get('status')}）===", flush=True)
            rec = {"round": rnd, "questions_count": len(qs), "questions": [],
                   "submit_http": None, "next_task": None, "next_final": None}
            if not qs:
                converged_round = rnd - 1
                print("[t2] 无待答问题 → 已收敛", flush=True)
                out["rounds"].append(rec)
                break

            for q in qs:
                opts = q.get("options") or []
                print(f"      [{q.get('priority','?')}] field={q.get('field','')} "
                      f"options={len(opts)} :: {str(q.get('question',''))[:64]}", flush=True)

            ans = await gen_answers(qs, prd)
            answers_payload: dict[str, str] = {}
            for q in qs:
                qid = str(q.get("id", ""))
                a = ans.get(qid) or {}
                text = (a.get("answer") or "").strip()
                opts = [str(o) for o in (q.get("options") or []) if isinstance(o, str)]
                src = a.get("source") or ""
                # 校验：带 options 的题若答案不在选项中，降级标注为 llm_from_original（不谎报来源）
                if opts and text not in opts:
                    src = "llm_from_original" if src == "system_options" else (src or "llm_from_original")
                if not text:
                    # 模型未给出 → 留空（不追问），显式标记为 missing，绝不脚本兜底
                    src = "missing"
                answers_payload[qid] = text
                all_answers[qid] = {"answer": text, "source": src,
                                    "doc_gap": bool(a.get("doc_gap")),
                                    "has_options": bool(opts),
                                    "priority": q.get("priority"), "field": q.get("field")}
                rec["questions"].append({
                    "id": qid, "priority": q.get("priority"), "field": q.get("field"),
                    "question": str(q.get("question", ""))[:400], "options": opts,
                    "answer": text, "source": src, "doc_gap": bool(a.get("doc_gap")),
                })
            print(f"      生成答案 {sum(1 for v in answers_payload.values() if v)}/{len(answers_payload)}；"
                  f"来源分布="
                  f"{ {s: sum(1 for v in all_answers.values() if v['source']==s) for s in ('system_options','llm_from_original','missing')} }",
                  flush=True)

            s = await c.post(f"{BASE}/requirements/{req_id}/clarify",
                             json={"answers": answers_payload}, headers=H)
            rec["submit_http"] = s.status_code
            print(f"      提交澄清 HTTP={s.status_code} {s.text[:150]}", flush=True)
            if s.status_code not in (200, 201):
                out["rounds"].append(rec)
                break
            nt = ((s.json().get("data") or {}).get("task_id")
                  or (s.json().get("data") or {}).get("id"))
            rec["next_task"] = nt
            if nt:
                print("      等待重解析完成...", flush=True)
                # 重解析含 LLM parse + FP 重提取；实测免费供应商单次调用可达 8 分钟+，
                # 故超时给到 25 分钟（超时不影响已提交答案与来源审计的取证）
                t2 = await poll_task(c, H, nt, timeout_s=1500)
                rec["next_final"] = {"status": t2.get("status"), "phase": t2.get("phase")}
            out["rounds"].append(rec)

        # 5) 终态
        g = await c.get(f"{BASE}/requirements/{req_id}/clarify/questions", headers=H)
        qd = (g.json().get("data") or {}) if g.status_code == 200 else {}
        remain = len(qd.get("questions") or [])
        out["final"] = {"status": qd.get("status"), "remaining_questions": remain,
                        "quality_report": qd.get("quality_report"),
                        "converged_round": converged_round}
        print(f"\n[t2] 终态: status={qd.get('status')} 剩余问题={remain}", flush=True)

        # 6) 来源审计
        srcs = [v["source"] for v in all_answers.values()]
        out["source_audit"] = {
            "total_answered": len(srcs),
            "by_source": {s: srcs.count(s) for s in set(srcs)},
            "script_driven_count": srcs.count("script_driven"),
            "missing_count": srcs.count("missing"),
            "doc_gap_count": sum(1 for v in all_answers.values() if v.get("doc_gap")),
            "detail": all_answers,
        }
        out["tr_summary"] = {
            "TR-2.1_parse_done": t.get("status") in ("completed", "done", "skipped"),
            "TR-2.2_submit_ok": all(r.get("submit_http") in (200, 201) for r in out["rounds"] if r.get("questions_count")),
            "TR-2.2_no_script_driven": srcs.count("script_driven") == 0,
            "TR-2.2_answered_gt_0": len([s for s in srcs if s in ("system_options", "llm_from_original")]) > 0,
            "TR-2.6_rounds_used": len(out["rounds"]),
            "TR-2.6_converged": remain == 0,
            "overall": (t.get("status") in ("completed", "done", "skipped")
                        and srcs.count("script_driven") == 0
                        and len([s for s in srcs if s in ("system_options", "llm_from_original")]) > 0),
        }

    _write(args.out, out)
    print(f"\n[t2] 产物已写入 {args.out}", flush=True)
    print(f"[t2] tr_summary={json.dumps(out['tr_summary'], ensure_ascii=False)}", flush=True)
    return 0 if out["tr_summary"]["overall"] else 1


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
