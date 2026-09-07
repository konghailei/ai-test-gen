"""诊断 clarify_agent 每个过滤环节的问题列表变化，看矛盾/引用未定义到底丢在哪一步"""
import sys, asyncio, json, os, time, types
sys.path.insert(0, '/app')

REQ_ID = "REQ-1786071385229-8899"

import logging
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
logging.getLogger("app.db.milvus_store").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.db.database import async_session
from app.db.models import Requirement
from app.agents.clarify_agent import ClarifyAgent, _dedup_questions_by_semantic_similarity, _filter_questions_by_necessity, _attach_semantic_tags, _select_questions_for_kb, MAX_KB_QUERIES, _apply_kb_answers

PATCH_STAGES = {}

def patch_stage(stage_name, raw_func, arg_idx_q=0):
    async def wrapped(*args, **kwargs):
        questions = args[arg_idx_q] if len(args) > arg_idx_q else kwargs.get("questions", [])
        before = list(questions)
        PATCH_STAGES[stage_name] = {"before": before}
        try:
            out = await raw_func(*args, **kwargs)
        except Exception as e:
            PATCH_STAGES[stage_name]["error"] = f"{type(e).__name__}: {e}"
            raise
        after = list(out) if isinstance(out, list) else out
        PATCH_STAGES[stage_name]["after"] = after
        PATCH_STAGES[stage_name]["dropped_ids"] = [q.get("id") for q in before if q.get("id") not in {qa.get("id") for qa in after if isinstance(after, list)}]
        print(f"\n  🛠️  [Stage: {stage_name}] before={len(before)} after={len(after)} dropped={len(PATCH_STAGES[stage_name]['dropped_ids'])}")
        for qid in PATCH_STAGES[stage_name]["dropped_ids"]:
            qd = next((q for q in before if q.get("id")==qid), None)
            if qd:
                print(f"      ❌ dropped {qid}: [{qd.get('priority')}/{qd.get('category')}] {str(qd.get('question',''))[:90]}")
        return out
    return wrapped

def patch_sync_stage(stage_name, raw_func, arg_idx_q=0):
    def wrapped(*args, **kwargs):
        questions = args[arg_idx_q] if len(args) > arg_idx_q else kwargs.get("questions", [])
        before = list(questions)
        PATCH_STAGES[stage_name] = {"before": before}
        try:
            out = raw_func(*args, **kwargs)
        except Exception as e:
            PATCH_STAGES[stage_name]["error"] = f"{type(e).__name__}: {e}"
            raise
        after = list(out) if isinstance(out, list) else out
        PATCH_STAGES[stage_name]["after"] = after
        PATCH_STAGES[stage_name]["dropped_ids"] = [q.get("id") for q in before if q.get("id") not in {qa.get("id") for qa in after if isinstance(after, list)}]
        print(f"\n  🛠️  [Stage: {stage_name}] before={len(before)} after={len(after)} dropped={len(PATCH_STAGES[stage_name]['dropped_ids'])}")
        for qid in PATCH_STAGES[stage_name]["dropped_ids"]:
            qd = next((q for q in before if q.get("id")==qid), None)
            if qd:
                print(f"      ❌ dropped {qid}: [{qd.get('priority')}/{qd.get('category')}] {str(qd.get('question',''))[:90]}")
        return out
    return wrapped

# Patch
from app.agents import clarify_agent as ca_mod
_orig_gen = ca_mod._generate_questions
_orig_dedup = ca_mod._dedup_questions_by_semantic_similarity
_orig_filter = ca_mod._filter_questions_by_necessity
_orig_tag = ca_mod._attach_semantic_tags
_orig_select = ca_mod._select_questions_for_kb
_orig_kb_answer = ca_mod._query_kb_for_answers
_orig_apply = ca_mod._apply_kb_answers

async def patched_gen(*a, **kw):
    print("\n" + "="*70)
    print("📤 Stage 1: _generate_questions (LLM 原始输出)")
    print("="*70)
    out = await _orig_gen(*a, **kw)
    PATCH_STAGES["1_generate"] = {"after": out}
    for q in out:
        print(f"  ✅ {q.get('id')}: [{q.get('priority')}/{q.get('category')}] {str(q.get('question',''))[:100]}")
    return out

async def patched_dedup(*a, **kw):
    print("\n📤 Stage 2: _dedup (语义去重)")
    return await patch_stage("2_dedup", _orig_dedup)(*a, **kw)

def patched_filter(*a, **kw):
    print("\n📤 Stage 3: _filter_questions_by_necessity (必要性过滤，去猜想问题)")
    return patch_sync_stage("3_filter", _orig_filter)(*a, **kw)

def patched_tag(*a, **kw):
    print("\n📤 Stage 4: _attach_semantic_tags (加语义标签)")
    return patch_sync_stage("4_tag", _orig_tag)(*a, **kw)

def patched_select(*a, **kw):
    print("\n📤 Stage 5: _select_questions_for_kb (KB查询候选，上限MAX_KB_QUERIES=%d)" % MAX_KB_QUERIES)
    return patch_sync_stage("5_select_kb", _orig_select)(*a, **kw)

async def patched_kb_answer(*a, **kw):
    print("\n📤 Stage 6: _query_kb_for_answers (实际查KB)")
    candidates = list(kw.get("questions") or (a[0] if a else []))
    PATCH_STAGES["6_kb_query"] = {"before": [q.get("id") for q in candidates]}
    out = await _orig_kb_answer(*a, **kw)
    PATCH_STAGES["6_kb_query"]["after"] = out
    for qid, ans in (out or {}).items():
        sim = ans.get("similarity", 0) if isinstance(ans, dict) else 0
        hit = "✅" if sim >= 0.5 else "❌"
        print(f"  {hit} KB回答 {qid}: sim={sim:.3f} ans={str(ans.get('answer',''))[:70] if isinstance(ans, dict) else str(ans)[:70]}")
    return out

def patched_apply(*a, **kw):
    print("\n📤 Stage 7: _apply_kb_answers (用KB回答过滤/标为已答)")
    return patch_sync_stage("7_apply_kb", _orig_apply)(*a, **kw)

ca_mod._generate_questions = patched_gen
ca_mod._dedup_questions_by_semantic_similarity = patched_dedup
ca_mod._filter_questions_by_necessity = patched_filter
ca_mod._attach_semantic_tags = patched_tag
ca_mod._select_questions_for_kb = patched_select
ca_mod._query_kb_for_answers = patched_kb_answer
ca_mod._apply_kb_answers = patched_apply

async def main():
    t0 = time.time()
    async with async_session() as sess:
        row = await sess.get(Requirement, REQ_ID)
        srr = json.loads(row.srr_json)
        print(f"Loaded {REQ_ID}: {srr.get('requirement',{}).get('title','?')}")
    agent = ClarifyAgent()
    result = await agent.run(srr_json=srr, product_id=srr.get("product_id"), force=True)
    print(f"\n{'='*70}\n⏱️ 总耗时 {time.time()-t0:.1f}s | 最终结果: {len(result.get('questions', []))} 条")
    print("="*70)
    for q in result.get("questions", []):
        resolved = "  ⚠️ UNRESOLVED" if not q.get("resolved") and not q.get("answered") else "  ✅ auto"
        opts = " options=["+", ".join((str(o.get('label','')) if isinstance(o, dict) else str(o))[:20] for o in (q.get("options") or [])[:4])+"]" if q.get("options") else ""
        print(f"  ▶ {q.get('id')}: [{q.get('priority')}/{q.get('category')}] {str(q.get('question',''))[:100]}{opts}{resolved}")

asyncio.run(main())
