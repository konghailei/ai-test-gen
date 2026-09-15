# -*- coding: utf-8 -*-
"""端到端生成 + 内容回传审阅台。

与"断言式"测试的分工：
    - 脚本负责**驱动**：登录 → 调真实 /api/generate（SSE）→ 取产物 → 落盘；
    - 脚本**不判定业务正确性**，而是把系统生成的全部内容与证据
      （用例全文 / 审核报告 / 覆盖证据 / 发布决策 / 降级标记 / 入库状态）
      原样渲染成一份单文件 HTML 审阅台，由人逐条判断"是否符合预期"。

用法（在 ai-test-gen-api 容器内执行，脚本自己走 HTTP 调真实接口）：

    # 跑一次生成并生成审阅台
    docker exec ai-test-gen-api python /app/scripts/review_generation_run.py \
        --req-id REQ-1787723370912-1404

    # 只从已有任务结果重建审阅台（不重新生成，长任务中断后用）
    ... --task-id <analysis_task_id>

产物目录（默认 _review_runs/<时间戳>/）：
    review.html   审阅台（单文件，逐条判定 + 导出）
    run.json      本次运行的完整证据快照
    cases.json    生成用例全文
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_REQ_ID = "REQ-1787723370912-1404"
# ⚠️ 默认写容器内 /tmp：往 /app（宿主机挂载目录）写文件会触发 uvicorn reload，
# reload 会杀掉所有在飞的后台任务（本项目已知坑）。跑完用 docker cp 取到宿主机。
DEFAULT_OUT_ROOT = "/tmp/_review_runs"

# 与前端 PublicationPanel 同一套中文口径（后端 reason 码 → 人话）
REASON_LABELS = {
    "empty_generation": "本次没有产出任何用例",
    "invalid_case_content": "用例不满足可执行合同（缺步骤/断言）",
    "quality_gate_not_passed": "需求质量门禁未通过",
    "quality_gate_forced": "门禁是强制放行（forced_pass）",
    "review_llm_failed": "审核 LLM 调用失败，本次审核降级为规则预检",
    "review_not_verified": "审核结果未被验证",
    "review_case_count_mismatch": "审核用例数与实际产出不一致",
    "review_issues_unavailable": "审核问题清单不可用",
    "review_blockers": "审核发现 blocker 级问题",
    "review_gaps_unresolved": "覆盖缺口未补齐（补生成已达上限）",
    "coverage_unknown": "覆盖证据不足（用例未标注 SRR 要点）",
    "coverage_insufficient": "要点覆盖率不足 80%",
    "high_priority_coverage_incomplete": "P0/P1 要点未 100% 覆盖",
    "invalid_point_references": "用例引用了不存在的要点 ID",
    "generation_subtasks_failed": "有功能点/通道生成失败",
    "publication_blocked": "内容不合格，已禁止写入知识库",
    "fp_load_failed": "功能点加载失败",
    "fp_generate_failed": "部分功能点定向生成失败",
    "batch_generate_failed": "全量兜底生成失败",
    "integration_generate_failed": "跨功能点/集成用例生成失败",
    "consistency_db_failed": "一致性对比的历史基线查询失败",
    "semantic_layer_failed": "澄清语义层失败（仅规则层生效）",
    "kb_publication_incomplete": "知识库写入不完整",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ① 驱动：登录 + 生成（SSE）
# ---------------------------------------------------------------------------

async def login(base_url: str, username: str, password: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=30.0, read=240.0, write=60.0)) as c:
        r = await c.post(f"{base_url}/api/auth/login",
                         json={"username": username, "password": password})
        r.raise_for_status()
        data = r.json().get("data") or {}
        token = data.get("token") or data.get("access_token")
        if not token:
            raise SystemExit(f"登录返回无 token: {str(data)[:200]}")
        return token


async def run_generation(base_url: str, token: str, req_id: str, timeout_min: int) -> tuple[dict, list[dict]]:
    """调 /api/generate 并消费 SSE，返回 (done 事件, 全过程事件)。"""
    import httpx

    events: list[dict] = []
    done: dict = {}
    payload = {"req_id": req_id}
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    timeout = httpx.Timeout(timeout_min * 60, connect=30.0, read=timeout_min * 60, write=60.0)

    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream("POST", f"{base_url}/api/generate", json=payload, headers=headers) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="ignore")[:300]
                raise SystemExit(f"/api/generate HTTP {r.status_code}: {body}")
            event_name = ""
            async for line in r.aiter_lines():
                if not line:
                    continue
                # ⚠️ SSE 事件名与 data 是两行，必须跨行携带（曾因把两者当独立行解析白跑 3 次）
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event.setdefault("_sse_event", event_name)
                events.append(event)
                stage = event.get("stage", "")
                if stage == "generate":
                    subs = event.get("subtasks") or []
                    if subs:
                        done_n = sum(1 for s in subs if s.get("status") in ("done", "resumed"))
                        log(f"  generate: {done_n}/{len(subs)} 功能点完成")
                    elif event.get("message"):
                        log(f"  generate: {event['message'][:60]}")
                elif stage:
                    log(f"  {stage}: {event.get('progress', '')}% {str(event.get('message', ''))[:50]}")
                if stage == "done":
                    done = event
                    break
                if stage == "error":
                    raise SystemExit(f"流水线返回 error: {event.get('message')}")
    return done, events


async def load_task_result(task_id: str) -> dict:
    """从 analysis_tasks.result_json + 落库用例重建（长任务中断后不重跑生成用）。

    注意：result_json 里**没有用例全文**（只有 case_count 等统计），
    因此用例正文从 test_cases 按任务时间窗回捞 —— 这是"重建"而不是"复现"，
    审阅台里会标注来源，避免误以为又重新跑了一次生成。
    """
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import AnalysisTask

    async with async_session() as s:
        row = (await s.execute(select(AnalysisTask).where(AnalysisTask.id == task_id))).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"任务不存在: {task_id}")
        result = row.result_json or {}
        window_start = row.created_at
        cases = await _cases_in_window(row.req_id, window_start)
        done = {
            "completion_status": result.get("completion_status", ""),
            "publication": result.get("publication") or {},
            "review_report": result.get("review_report") or {},
            "degraded_flags": result.get("degraded_flags") or [],
            "failed_subtasks": result.get("failed_subtasks") or [],
            "kb_flowback": result.get("kb_flowback") or {},
            "manual_action_required": result.get("manual_action_required"),
            "duration_ms": 0,
            "cases": cases,
        }
        return {"task_id": row.id, "status": row.status, "result_json": result, "done_event": done}


async def _cases_in_window(req_id: str, since) -> list[dict]:
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import TestCase

    async with async_session() as s:
        stmt = select(TestCase).where(TestCase.req_id == req_id)
        if since is not None:
            stmt = stmt.where(TestCase.created_at >= since)
        rows = (await s.execute(stmt)).scalars().all()
        return [dict(r.case_json or {}) for r in rows]


async def fetch_persisted_cases(req_id: str, since: datetime | None) -> list[dict]:
    """取本次运行真正落库的用例状态（生成 ≠ 入库，这里看的是库里的真相）。"""
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import TestCase

    async with async_session() as s:
        stmt = select(TestCase).where(TestCase.req_id == req_id)
        if since is not None:
            stmt = stmt.where(TestCase.created_at >= since)
        rows = (await s.execute(stmt)).scalars().all()
        return [{
            "id": r.id, "title": r.title, "type": r.type, "status": r.status,
            "confidence": r.confidence, "source": r.source, "gen_batch_id": r.gen_batch_id,
            "weight": r.weight, "execution_count": r.execution_count,
            "archived_at": r.archived_at.isoformat() if r.archived_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


async def fetch_requirement(req_id: str) -> dict:
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import FunctionalPoint, Requirement

    async with async_session() as s:
        req = (await s.execute(select(Requirement).where(Requirement.id == req_id))).scalar_one_or_none()
        fps = (await s.execute(select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id))).scalars().all()
        if req is None:
            raise SystemExit(f"需求不存在: {req_id}")
        return {
            "id": req.id, "title": req.title, "status": req.status,
            "product_id": req.product_id, "priority": req.priority,
            "version": getattr(req, "version", ""),
            "srr_len": len(json.dumps(req.srr_json or {}, ensure_ascii=False)),
            "fps": [{"id": f.id, "name": f.name, "priority": f.priority,
                     "srr_len": len(json.dumps(f.srr_json or {}, ensure_ascii=False))} for f in fps],
        }


# ---------------------------------------------------------------------------
# ② 回传：单文件 HTML 审阅台
# ---------------------------------------------------------------------------

def _esc(text) -> str:
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_html(payload: dict) -> str:
    data_js = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>生成结果审阅台</title>
<style>
:root{--ink:#1f2937;--ink2:#4b5563;--line:#e5e7eb;--bg:#f8fafc;--card:#fff;
--ok:#059669;--warn:#d97706;--bad:#dc2626;--blue:#2563eb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 80px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--line)}
.sub{color:var(--ink2);font-size:12px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.kv{font-size:12px;color:var(--ink2)}
.kv b{display:block;font-size:18px;color:var(--ink);margin-top:2px;font-weight:650}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.b-ok{background:#ecfdf5;color:var(--ok)}.b-warn{background:#fffbeb;color:var(--warn)}
.b-bad{background:#fef2f2;color:var(--bad)}.b-info{background:#eff6ff;color:var(--blue)}
.b-gray{background:#f3f4f6;color:var(--ink2)}
.reasons{margin:8px 0 0;padding-left:18px;font-size:13px}
.reasons li{margin:2px 0}
.case{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);
border-radius:12px;padding:14px;margin-bottom:12px}
.case[data-verdict="ok"]{border-left-color:var(--ok)}
.case[data-verdict="bad"]{border-left-color:var(--bad)}
.case[data-verdict="doubt"]{border-left-color:var(--warn)}
.chead{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.ctitle{font-weight:650;font-size:14px}
.meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{background:#f3f4f6;color:var(--ink2);border-radius:6px;padding:1px 7px;font-size:11.5px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12.5px}
th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{background:#f9fafb;font-weight:600;color:var(--ink2)}
.verdict{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:10px;
padding-top:9px;border-top:1px dashed var(--line);font-size:12.5px}
.verdict label{cursor:pointer}
textarea{width:100%;min-height:38px;border:1px solid var(--line);border-radius:8px;
padding:6px 8px;font:12.5px/1.5 inherit;resize:vertical}
.toolbar{position:sticky;top:0;z-index:9;background:rgba(248,250,252,.96);
border-bottom:1px solid var(--line);padding:10px 0;margin-bottom:6px;display:flex;
gap:10px;align-items:center;flex-wrap:wrap}
button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;
font-size:12.5px;cursor:pointer}
button:hover{border-color:var(--blue);color:var(--blue)}
pre{background:#0f172a;color:#e2e8f0;border-radius:10px;padding:12px;overflow:auto;
max-height:420px;font-size:11.5px;line-height:1.5}
details{margin-bottom:10px}
summary{cursor:pointer;font-weight:600;font-size:13px}
.muted{color:var(--ink2);font-size:12px}
.pill{font-size:11px;padding:1px 6px;border-radius:5px;background:#f3f4f6;color:var(--ink2)}
</style></head><body><div class="wrap">
<h1>生成结果审阅台</h1>
<div class="sub" id="subtitle"></div>
<div class="toolbar">
  <span class="muted">逐条判定：符合预期 / 不符预期 / 存疑（自动保存在本机浏览器）</span>
  <span id="tally" class="muted"></span>
  <button onclick="exportVerdicts()">导出判定 JSON</button>
  <button onclick="clearVerdicts()">清空判定</button>
</div>
<div id="root"></div>
<script>
const DATA = __DATA__;
const LS_KEY = 'review-' + DATA.run_id;
let verdicts = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function save(){ localStorage.setItem(LS_KEY, JSON.stringify(verdicts)); renderTally(); }
function setVerdict(id, v){ verdicts[id] = verdicts[id]||{}; verdicts[id].verdict = v; save(); }
function setNote(id, t){ verdicts[id] = verdicts[id]||{}; verdicts[id].note = t; save(); }
function renderTally(){
  const vals = Object.values(verdicts); const n = k => vals.filter(v=>v.verdict===k).length;
  document.getElementById('tally').textContent =
    `已判定 ${vals.filter(v=>v.verdict).length}/${DATA.cases.length} · 符合 ${n('ok')} · 不符 ${n('bad')} · 存疑 ${n('doubt')}`;
}
function exportVerdicts(){
  const out = {run_id: DATA.run_id, exported_at: new Date().toISOString(),
    summary: {ok:0,bad:0,doubt:0,undecided:0}, items: []};
  DATA.cases.forEach(c => {
    const v = verdicts[c.id] || {};
    out.summary[v.verdict || 'undecided']++;
    out.items.push({id:c.id, title:c.title, verdict:v.verdict||'', note:v.note||''});
  });
  const blob = new Blob([JSON.stringify(out,null,2)],{type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'verdicts-' + DATA.run_id + '.json'; a.click();
}
function clearVerdicts(){
  if(!confirm('清空本机保存的判定？')) return;
  verdicts = {}; save(); location.reload();
}

function chips(list){ return `<div class="meta">${list.map(x=>`<span class="chip">${esc(x)}</span>`).join('')}</div>`; }
function stepsTable(steps){
  if(!steps || !steps.length) return '<p class="muted">（无步骤）</p>';
  return `<table><thead><tr><th style="width:44px">#</th><th>动作 action</th><th>预期 expected</th><th>数据 data</th></tr></thead><tbody>` +
    steps.map((s,i)=>`<tr><td>${esc(s.step ?? i+1)}</td><td>${esc(s.action)}</td><td>${esc(s.expected)}</td><td class="muted">${esc(typeof s.data==='object'?JSON.stringify(s.data):s.data)}</td></tr>`).join('') +
    '</tbody></table>';
}
function assertTable(list){
  if(!list || !list.length) return '<p class="muted">（无断言 —— 注意：这会违反可执行合同）</p>';
  return `<table><thead><tr><th>type</th><th>target</th><th>expected_value</th><th>message</th></tr></thead><tbody>` +
    list.map(a => typeof a === 'string'
      ? `<tr><td colspan="4">${esc(a)}</td></tr>`
      : `<tr><td>${esc(a.type)}</td><td>${esc(a.target)}</td><td>${esc(a.expected_value)}</td><td class="muted">${esc(a.message)}</td></tr>`).join('') +
    '</tbody></table>';
}
function renderCase(c, idx){
  const v = verdicts[c.id] || {};
  const pts = c.extracted_point_ids || [];
  const cover = pts.length ? pts.join(', ') : '（未标注）';
  const flags = [];
  if(!(c.assertions||[]).length) flags.push('缺断言');
  if(!(c.steps||[]).length) flags.push('缺步骤');
  if(!pts.length) flags.push('未标注覆盖要点');
  if(c.annotation_heuristic) flags.push('覆盖为启发式回填（仅线索）');
  if((c.confidence||0) < 0.7) flags.push('低置信度');
  return `<div class="case" data-verdict="${esc(v.verdict||'')}" id="case-${esc(c.id)}">
  <div class="chead">
    <div><span class="ctitle">${idx+1}. ${esc(c.title)}</span>
      ${flags.map(f=>`<span class="badge b-warn" style="margin-left:6px">${esc(f)}</span>`).join('')}
    </div>
    <span class="pill">${esc(c.id)}</span>
  </div>
  ${chips([`类型 ${c.type||'-'}`, `优先级 ${c.priority||'-'}`, `置信度 ${c.confidence??'-'}`,
           `关联FP ${c.related_fp||'-'}`, `关联需求 ${c.related_requirement||'-'}`,
           `入库状态 ${c.persisted_status||'（未见落库）'}`, `来源 ${c.source||'-'}`])}
  <div class="meta muted">覆盖要点（按后端口径解析）：${esc(cover)}${c.annotation_heuristic?' ｜ <b>启发式回填（仅线索，非证据）</b>':''}
    ${c.notes?`<br>notes：${esc(c.notes)}`:''}</div>
  ${(c.preconditions||[]).length ? `<div class="meta muted">前置条件：${esc((c.preconditions||[]).join('；'))}</div>` : ''}
  ${c.description ? `<div class="meta muted">描述：${esc(c.description)}</div>` : ''}
  <div><b class="muted">步骤</b>${stepsTable(c.steps)}</div>
  <div><b class="muted">断言</b>${assertTable(c.assertions)}</div>
  <div class="verdict">
    <span class="muted">你的判定：</span>
    ${[['ok','符合预期'],['bad','不符预期'],['doubt','存疑']].map(([k,label])=>
      `<label><input type="radio" name="v-${esc(c.id)}" ${v.verdict===k?'checked':''}
        onchange="setVerdict('${esc(c.id)}','${k}')"> ${label}</label>`).join('')}
  </div>
  <textarea placeholder="备注：哪里不符合预期 / 期望应该是什么（可选）"
    oninput="setNote('${esc(c.id)}', this.value)">${esc(v.note||'')}</textarea>
</div>`;
}

function render(){
  const d = DATA;
  document.getElementById('subtitle').textContent =
    `${d.requirement.title}（${d.requirement.id}） · 运行 ${d.run_id} · ${d.started_at} · 耗时 ${d.duration_ms}ms`;
  const tone = d.outcome.status === 'completed' ? 'b-ok' : d.outcome.status === 'failed' ? 'b-bad' : 'b-warn';
  let html = '';
  if (d.workspace_note) {
    html += `<div class="card" style="border-left:4px solid var(--blue);margin-bottom:14px">
      <b>说明</b><div class="muted" style="margin-top:4px">${esc(d.workspace_note)}</div></div>`;
  }
  html += `<h2>一、总览</h2><div class="grid">
    <div class="card kv">任务终态<b><span class="badge ${tone}">${esc(d.outcome.status)}</span></b></div>
    <div class="card kv">发布决策<b><span class="badge ${d.outcome.decision==='auto_approve'?'b-ok':d.outcome.decision==='blocked'?'b-bad':'b-warn'}">${esc(d.outcome.decision||'-')}</span></b></div>
    <div class="card kv">生成用例<b>${d.cases.length}</b></div>
    <div class="card kv">落库分布<b>${esc(d.persisted_summary)}</b></div>
    <div class="card kv">KB 回流 auto/pending<b>${esc(d.kb_summary)}</b></div>
    <div class="card kv">覆盖口径<b><span class="badge ${d.coverage.status==='mapped'?'b-ok':'b-warn'}">${esc(d.coverage.status)}</span></b>
      <div class="muted">标注率 ${d.coverage.annotation_rate} · 已证实覆盖 ${d.coverage.observed_rate}（${d.coverage.covered}/${d.coverage.total}）· 未覆盖 ${d.coverage.uncovered}</div></div>
  </div>`;

  if (d.outcome.reasons.length) {
    html += `<h2>二、为什么没有自动入库（发布决策原因）</h2><ul class="reasons">` +
      d.outcome.reasons.map(r=>`<li><b>${esc(r)}</b> — ${esc(d.reason_labels[r]||'')}</li>`).join('') + '</ul>';
  } else {
    html += `<h2>二、发布决策</h2><p class="muted">无原因（直接 auto_approve）。</p>`;
  }
  if (d.outcome.degraded_flags.length) {
    html += `<p class="muted">降级标记：` + d.outcome.degraded_flags.map(f=>`<span class="badge b-warn">${esc(f)}</span> ${esc(d.reason_labels[f]||'')}`).join(' ； ') + '</p>';
  }
  if (d.outcome.failed_subtasks.length) {
    html += `<p class="muted">失败子任务：${esc(JSON.stringify(d.outcome.failed_subtasks).slice(0,300))}</p>`;
  }

  html += `<h2>三、生成用例逐条判定（${d.cases.length} 条）</h2>`;
  html += d.cases.length ? d.cases.map(renderCase).join('') : '<p class="muted">本次没有产出用例。</p>';

  html += `<h2>四、原始证据（可展开核对）</h2>
    <details><summary>审核报告 review_report</summary><pre>${esc(JSON.stringify(d.review_report,null,2))}</pre></details>
    <details><summary>覆盖证据 point_coverage</summary><pre>${esc(JSON.stringify(d.point_coverage,null,2))}</pre></details>
    <details><summary>知识库回流 kb_flowback</summary><pre>${esc(JSON.stringify(d.kb_flowback,null,2))}</pre></details>
    <details><summary>需求与功能点</summary><pre>${esc(JSON.stringify(d.requirement,null,2))}</pre></details>
    <details><summary>done 事件全文</summary><pre>${esc(JSON.stringify(d.done_event,null,2))}</pre></details>
    <details><summary>阶段事件序列</summary><pre>${esc(JSON.stringify(d.stage_trace,null,2))}</pre></details>`;
  document.getElementById('root').innerHTML = html;
  renderTally();
}
render();
</script></div></body></html>
""".replace("__DATA__", data_js)


# ---------------------------------------------------------------------------
# ③ 组装
# ---------------------------------------------------------------------------

def collect_case_payload(done: dict, persisted: list[dict]) -> list[dict]:
    """把 done 事件的用例与库里的落库状态合并，并**用后端同一套逻辑**解析覆盖标注。

    ⚠️ 必须复用 `extract_point_ids`：覆盖标注可能写在 notes（"覆盖: [x.1]"）、
    gen_metadata.covered_points、covered_point_ids 等多个位置。自定义一套解析
    会得出与后端不同的"未标注"结论 —— 审阅台若与系统口径不一致，就是在误导人。
    """
    from app.utils.coverage import extract_point_ids, is_heuristic_mapping

    by_id = {p["id"]: p for p in persisted}
    cases = []
    for c in done.get("cases") or []:
        p = by_id.get(c.get("id"), {})
        try:
            extracted = sorted(extract_point_ids(c))
        except Exception:  # noqa: BLE001
            extracted = []
        cases.append({
            **c,
            "extracted_point_ids": extracted,
            "annotation_heuristic": is_heuristic_mapping(c),
            "persisted_status": p.get("status", ""),
            "source": p.get("source", c.get("source", "")),
            "weight": p.get("weight"),
            "archived_at": p.get("archived_at"),
        })
    return cases


def reason_labels_subset(reasons: list[str]) -> dict:
    return {r: REASON_LABELS.get(r, "") for r in reasons}


async def build_payload(req_id: str, req: dict, done: dict, events: list[dict],
                        persisted: list[dict], persisted_new: list[dict],
                        started_iso: str, run_id: str,
                        workspace_note: str = "") -> dict:
    report = done.get("review_report") or {}
    publication = done.get("publication") or report.get("publication") or {}
    coverage = (report.get("point_coverage") or done.get("point_coverage") or {})
    kb = done.get("kb_flowback") or {}
    stage_trace = [{"stage": e.get("stage"), "progress": e.get("progress"),
                    "message": str(e.get("message", ""))[:120]} for e in events]
    st_counts: dict[str, int] = {}
    for p in persisted_new:
        st_counts[p["status"]] = st_counts.get(p["status"], 0) + 1
    return {
        "run_id": run_id,
        "started_at": started_iso,
        "duration_ms": done.get("duration_ms", 0),
        "workspace_note": workspace_note,
        "requirement": req,
        "outcome": {
            "status": done.get("completion_status", ""),
            "decision": publication.get("decision", ""),
            "reasons": publication.get("reasons") or [],
            "degraded_flags": done.get("degraded_flags") or [],
            "failed_subtasks": done.get("failed_subtasks") or [],
            "manual_action_required": done.get("manual_action_required"),
        },
        "reason_labels": reason_labels_subset(publication.get("reasons") or []),
        "coverage": {
            "status": coverage.get("coverage_status", "unknown"),
            "annotation_rate": coverage.get("annotation_rate"),
            "observed_rate": coverage.get("observed_coverage_rate", coverage.get("coverage_rate")),
            "covered": coverage.get("covered_points"),
            "total": coverage.get("total_points"),
            "uncovered": len(coverage.get("uncovered") or []),
            "heuristic_point_ids": coverage.get("heuristic_point_ids") or [],
            "invalid_point_ids": coverage.get("invalid_point_ids") or [],
        },
        "point_coverage": coverage,
        "review_report": report,
        "kb_flowback": kb,
        "done_event": {k: v for k, v in done.items() if k != "cases"},
        "stage_trace": stage_trace,
        "cases": [],  # 由调用方填充
        "persisted": persisted,
        "persisted_summary": " / ".join(f"{k}={v}" for k, v in sorted(st_counts.items())) or "无落库",
        "kb_summary": f"{kb.get('auto_approved', 0)}/{kb.get('pending_review', 0)}" + ("（已跳过）" if kb.get("skipped") else ""),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="端到端生成 + 内容回传审阅台")
    ap.add_argument("--req-id", default=DEFAULT_REQ_ID)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="888888")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--task-id", default="", help="不重新生成，从已有任务结果重建审阅台")
    ap.add_argument("--from-run", default="",
                    help="从已保存的 run.json 重新渲染审阅台（不重新生成、不连库）")
    ap.add_argument("--note", default="", help="附加说明，写入审阅台头部（如口径修复备注）")
    ap.add_argument("--timeout-min", type=int, default=40, help="生成超时（分钟）")
    args = ap.parse_args()

    # 重新渲染模式：只按后端口径重算标注并重出 HTML（改过审阅台模板后用它，避免重跑生成）
    if args.from_run:
        with open(args.from_run, encoding="utf-8") as f:
            payload = json.load(f)
        out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.from_run))
        payload["cases"] = collect_case_payload({"cases": payload.get("cases") or []},
                                                payload.get("persisted") or [])
        if args.note:
            payload["workspace_note"] = args.note
        html_path = os.path.join(out_dir, "review.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(build_html(payload))
        with open(os.path.join(out_dir, "cases.json"), "w", encoding="utf-8") as f:
            json.dump(payload["cases"], f, ensure_ascii=False, indent=2, default=str)
        annotated = sum(1 for c in payload["cases"] if c.get("extracted_point_ids"))
        log(f"重渲染完成：{html_path}")
        log(f"用例 {len(payload['cases'])} 条，按后端口径有覆盖标注 {annotated} 条")
        print(f"OUT_DIR={out_dir}")
        return 0

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out_dir or os.path.join("/app/_review_runs", run_id)
    os.makedirs(out_dir, exist_ok=True)
    started_iso = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    started_dt = datetime.now(timezone.utc)

    req = await fetch_requirement(args.req_id)
    log(f"需求：{req['title']}（{req['id']}）功能点 {len(req['fps'])} 个 · SRR {req['srr_len']} 字符")

    if args.task_id:
        log(f"从已有任务重建：{args.task_id}")
        task = await load_task_result(args.task_id)
        done = task["done_event"]
        events = []
        note = f"由任务 {args.task_id}（status={task['status']}）重建，未重新调用生成"
    else:
        log("登录…")
        token = await login(args.base_url, args.username, args.password)
        log("开始生成（真实 LLM 链路，长任务；每阶段会打印进度）…")
        done, events = await run_generation(args.base_url, token, args.req_id, args.timeout_min)
        note = ""
    if not done:
        log("⚠️ 未收到 done 事件（可能超时/中断）；可用 --task-id 从任务结果重建")
        return 2

    persisted = await fetch_persisted_cases(args.req_id, None)
    persisted_new = [p for p in persisted if _parse_dt(p.get("created_at")) and _parse_dt(p["created_at"]) >= started_dt]
    if args.task_id:
        persisted_new = persisted  # 重建模式下无法区分新旧，如实标注
    payload = await build_payload(args.req_id, req, done, events, persisted, persisted_new,
                                  started_iso, run_id, note)
    payload["cases"] = collect_case_payload(done, persisted)

    html_path = os.path.join(out_dir, "review.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(payload))
    with open(os.path.join(out_dir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "cases.json"), "w", encoding="utf-8") as f:
        json.dump(payload["cases"], f, ensure_ascii=False, indent=2, default=str)

    out = payload["outcome"]
    log("=" * 66)
    log(f"终态={out['status']}  发布决策={out['decision']}  用例={len(payload['cases'])} 条")
    log(f"发布原因：{out['reasons'] or '无'}")
    log(f"覆盖口径={payload['coverage']['status']}  标注率={payload['coverage']['annotation_rate']}  "
        f"已证实覆盖={payload['coverage']['observed_rate']}（{payload['coverage']['covered']}/{payload['coverage']['total']}）")
    log(f"落库：{payload['persisted_summary']} ｜ KB 回流：{payload['kb_summary']}")
    log(f"降级标记：{out['degraded_flags'] or '无'}")
    log(f"审阅台：{html_path}")
    log("=" * 66)
    print(f"OUT_DIR={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
