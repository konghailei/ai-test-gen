"""真实数据检查：REQ-1785821750443-7544 的澄清问题、KB 查询、FP 复用情况"""
import asyncio
import json
import os
import sys

# 允许在 backend 目录下直接运行（无需 docker /app）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db.database import async_session
from app.db.models import Requirement, FunctionalPoint, AnalysisTask
from sqlalchemy import select

REQ_ID = "REQ-1785821750443-7544"


async def dump_related_fps(s, product_id, project_id, module_ids):
    """列出同产品/同模块的其他 FP，供"是否应该复用而没有复用"判断。"""
    clauses = []
    if product_id:
        clauses.append(Requirement.product_id == product_id)
    if project_id:
        clauses.append(Requirement.project_id == project_id)
    # 按需求表过滤 FP
    stmt = select(FunctionalPoint).join(Requirement, FunctionalPoint.requirement_id == Requirement.id)
    for c in clauses:
        stmt = stmt.where(c)
    rows = (await s.execute(stmt)).scalars().all()
    print(f"\n=== 同产品/项目关联 FP 总数: {len(rows)} ===")
    # 筛选 module 交集
    related = []
    for f in rows:
        mods = set(f.modules or [])
        if f.module_id:
            mods.add(f.module_id)
        inter = mods & set(module_ids or [])
        if module_ids and inter:
            related.append((f, sorted(inter)))
        elif not module_ids:
            related.append((f, sorted(mods)))
    print(f"    |-- 与当前需求模块有交集的 FP: {len(related)}")
    for f, inter in related[:50]:
        print(
            f"      - {f.id}  name={f.name!r:60}  type={f.fp_type:18}  "
            f"req={f.requirement_id}  status={f.status}  shared_mods={inter}"
        )
    if len(related) > 50:
        print(f"      ... (剩余 {len(related) - 50} 条省略)")
    return related


async def main():
    async with async_session() as s:
        r = await s.get(Requirement, REQ_ID)
        if not r:
            print(f"NOT FOUND: {REQ_ID}")
            return

        srr = r.srr_json if r.srr_json else {}
        req_meta = srr.get("requirement", {}) or {}
        modules = req_meta.get("modules") or []

        print(f"=== {REQ_ID} 基础信息 ===")
        print(f"    status={r.status}  product_id={r.product_id}  project_id={r.project_id}")
        print(f"    title={r.title}")
        print(f"    modules={modules}")
        print(f"    go_live_date={r.go_live_date}  archived_at={r.archived_at}")

        print(f"\n=== 需求 SRR.requirement 摘要 ===")
        for k in ("id", "title", "summary", "priority", "product_name", "change_type", "project_id"):
            v = req_meta.get(k)
            if v:
                print(f"    {k}: {str(v)[:200]}")

        clarify_state = srr.get("_clarify_state") or {}
        print(f"\n=== _clarify_state 结构 ===")
        print(f"    keys={list(clarify_state.keys())}")
        qs = clarify_state.get("questions") or []
        print(f"    questions 总数={len(qs)}")
        for i, q in enumerate(qs[:30]):
            if isinstance(q, dict):
                print(
                    f"      [{i}] id={q.get('id'):30}  pri={q.get('priority'):10}  "
                    f"cat={q.get('category'):15}  field={q.get('field')}"
                )
                if q.get("question"):
                    print(f"           Q: {str(q.get('question'))[:140]}")
        if len(qs) > 30:
            print(f"      ... 剩余 {len(qs) - 30} 条省略")

        print(f"\n=== clarify_answers (用户已回答) ===")
        ca = srr.get("clarify_answers") or {}
        print(f"    已回答字段数={len(ca)}")
        for k, v in list(ca.items())[:20]:
            print(f"      {k}: {str(v)[:140]}")

        print(f"\n=== _clarify_history 轮次 ===")
        ch = srr.get("_clarify_history") or []
        print(f"    轮次记录: {len(ch)}")
        for i, entry in enumerate(ch):
            action = entry.get("action") if isinstance(entry, dict) else None
            ts = entry.get("timestamp") if isinstance(entry, dict) else None
            print(f"      [{i}] action={action}  ts={ts}  keys={list(entry.keys()) if isinstance(entry, dict) else []}")

        kb_status = clarify_state.get("kb_status") or {}
        print(f"\n=== kb_status ===")
        print(json.dumps(kb_status, ensure_ascii=False, indent=6))

        kb_access = clarify_state.get("kb_access_log") or []
        print(f"\n=== kb_access_log 条目数={len(kb_access)} ===")
        for i, k in enumerate(kb_access[:40]):
            if isinstance(k, dict):
                print(
                    f"      [{i:2d}] action={k.get('action'):18}  type={k.get('type'):16}  "
                    f"answered={k.get('answered')}  count={k.get('result_count')}  "
                    f"dur_ms={k.get('duration_ms')}  status={k.get('status')}"
                )
                if k.get("query"):
                    print(f"           Q: {str(k.get('query'))[:160]}")
                if k.get("sources"):
                    src_ids = [
                        (s if isinstance(s, str) else s.get("req_id") or s.get("id") or "?")
                        for s in (k.get("sources") or [])[:6]
                    ]
                    print(f"           srcs[:6]: {src_ids}")
        if len(kb_access) > 40:
            print(f"      ... 剩余 {len(kb_access) - 40} 条省略")

        print(f"\n=== 生成的 FunctionalPoint（当前需求入库的）===")
        fps = (
            await s.execute(
                select(FunctionalPoint)
                .where(FunctionalPoint.requirement_id == REQ_ID)
                .order_by(FunctionalPoint.created_at.asc())
            )
        ).scalars().all()
        print(f"    数量={len(fps)}")
        for i, f in enumerate(fps):
            mods = set(f.modules or [])
            if f.module_id:
                mods.add(f.module_id)
            print(
                f"      [{i:2d}] id={f.id}  name={f.name!r:60}  type={f.fp_type:18}  "
                f"status={f.status:16}  mods={sorted(mods)[:4]}  srr={bool(f.srr_json)}"
            )
            # 检查 srr_json 里是否有 reuse_existing / 复用标记
            if isinstance(f.srr_json, dict):
                sj = f.srr_json
                flags = {
                    k: sj.get(k) for k in ("reuse_existing", "reused_from_fp_id", "existing_fp_id", "reused", "from_existing")
                    if sj.get(k) is not None
                }
                if flags:
                    print(f"           reuse flags: {flags}")

        # 分析 tasks（看需求分析/澄清阶段 task 产物）
        print(f"\n=== 最近 5 条 AnalysisTask（type=upload_requirement/submit_clarify）===")
        tasks = (
            await s.execute(
                select(AnalysisTask)
                .where(AnalysisTask.req_id == REQ_ID)
                .order_by(AnalysisTask.created_at.desc())
                .limit(5)
            )
        ).scalars().all()
        for t in tasks:
            rj = t.result_json or {}
            print(
                f"    - id={t.id}  type={t.task_type}  status={t.status}  "
                f"phase={t.phase}  progress={t.progress}  msg={(t.message or '')[:60]}"
            )
            summary_keys = [k for k in ("case_count", "type_dist", "kb_status", "clarify_questions_count",
                                        "kb_answered_count", "kb_total_count", "has_unanswered_questions", "kb_empty")
                            if rj.get(k) is not None]
            if summary_keys:
                print(f"         result_json 摘要 keys={summary_keys}")
                for k in summary_keys:
                    v = rj[k]
                    if isinstance(v, dict):
                        print(f"             {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
                    else:
                        print(f"             {k}: {v}")

        print(f"\n=== 同产品/项目/模块 现有 FP（供复用判断） ===")
        await dump_related_fps(s, r.product_id, r.project_id, modules)


if __name__ == "__main__":
    asyncio.run(main())
