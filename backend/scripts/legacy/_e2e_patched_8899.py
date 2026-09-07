"""绕过 LLM API Key 限制跑完整 clarify() 流程：
   Patch _generate_kb_queries 输出空 queries（或规则层兜底 queries），
   让 clarify() 能跑到最后，验证最终 questions 里的 id 前缀：
   - ref_undef（ambiguity）应该带 kb_ 前缀（证明先 KB 未答才转人工）
   - contradiction（conflict）应该不带 kb_ 前缀（直接人工）
   - 所有 options 都存在（人工问题有建议答案）
"""
import sys, asyncio, json, logging, re
sys.path.insert(0, '/app')

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
logging.getLogger("app.agents.clarify_agent").setLevel(logging.INFO)

from app.db.database import async_session
from app.db.models import Requirement
from app.agents.clarify_agent import ClarifyAgent
from sqlalchemy import select

REQ_ID = "REQ-1786071385229-8899"
HEALTHY_ID = "REQ-1786068597783-4184"

_orig_generate_kb_queries = ClarifyAgent._generate_kb_queries
async def _patched_generate_kb_queries(self, srr, completeness, is_inc, kb_provider=None):
    """LLM 不可用时的规则层极简 KB query 兜底：
       只对 completeness 里的 important/critical missing 生成简单查询。
       这样整个流水线能跑到最后，不会因 API Key 中断。"""
    import hashlib
    qs = []
    for gap in (completeness.get("important_missing", []) or []) + (completeness.get("critical_missing", []) or []):
        f = gap.get("field", "gap")
        desc = gap.get("description", f) or f
        # 只选和规则层问题相关的几项，避免太多噪音 + 超过MAX_KB_QUERIES=8
        q = f"请问在同类需求中，{f} 通常如何定义？（补充：{desc[:80]}）"
        qs.append({"field": f, "missing_desc": desc, "priority": gap.get("priority","important"),
                   "question": q, "_is_baseline": True})
    logging.getLogger("app.agents.clarify_agent").info(
        "[PATCHED _generate_kb_queries] LLM 不可用，规则层兜底生成 %d 条 KB 查询基线", len(qs),
    )
    return qs

_orig_query_kb = ClarifyAgent._query_kb_for_answers
async def _patched_query_kb(self, srr, kb_questions):
    """KB 检索也 Mock：所有 KB 查询都返回 unanswered（因为我们要验证「KB未答才转人工」的链路）。
    同时也记录哪些 kb_question 来自「统一分流池转来的ref_undef」，用于验证。"""
    out = []
    for q in kb_questions:
        out.append({
            "field": q.get("field"),
            "question": q.get("question", ""),
            "missing_desc": q.get("missing_desc", ""),
            "answered": False,   # 全部未答，用于验证后续「KB未答转人工」链路完整
            "sources": [],
            "answer": "",
            "_from_llm_semantic": q.get("_from_llm_semantic", False),
            "priority": q.get("priority", "important"),
        })
    logging.getLogger("app.agents.clarify_agent").info(
        "[PATCHED _query_kb_for_answers] 全部 %d 条 KB 查询视为未答（模拟KB中无历史定义场景），进入人工链路", len(out),
    )
    return out

_orig_refine = ClarifyAgent._refine_unresolved_to_questions
async def _patched_refine(self, questions, srr, kb_provider=None):
    """refine 也 patch 掉：LLM不可用时直接返回原questions（已通过_build_field_question补过options了）。"""
    logging.getLogger("app.agents.clarify_agent").info(
        "[PATCHED _refine] LLM 不可用，跳过 LLM 精炼，保留 _build_field_question 模板已生成的 options。共 %d 条", len(questions),
    )
    return questions

ClarifyAgent._generate_kb_queries = _patched_generate_kb_queries
ClarifyAgent._query_kb_for_answers = _patched_query_kb
ClarifyAgent._refine_unresolved_to_questions = _patched_refine

async def verify(req_id, label):
    async with async_session() as session:
        r = (await session.execute(
            select(Requirement).where(Requirement.id == req_id)
        )).scalar_one_or_none()
        assert r, f"需求不存在: {req_id}"
        srr = r.srr_json if isinstance(r.srr_json, dict) else json.loads(r.srr_json)
        srr["_raw_text"] = r.raw_text or ""
        srr.setdefault("requirement", {})["id"] = r.id
        srr.setdefault("requirement", {})["title"] = r.title or req_id

    agent = ClarifyAgent()
    print("\n" + "="*90)
    print(f"🧪 [{label}] clarify({req_id}: {r.title or ''}) — LLM API Key 全 patched 为规则兜底")
    print("="*90)

    async def _cb(msg, prog):
        pass

    try:
        result = await agent.clarify(srr, session=None, progress_callback=_cb)
    except Exception as e:
        import traceback
        print(f"❌ clarify 仍异常: {type(e).__name__}: {str(e)[:300]}")
        traceback.print_exc(limit=3)
        return

    qs = result.get("questions", []) or []
    print(f"\n✅ clarify() 跑完！最终 questions: {len(qs)} 条")
    print(f"   status={result.get('status')}  kb_answered={result.get('kb_answered_count')}/{result.get('kb_total_count')}")
    print("-"*90)

    kb_questions = []
    direct_questions = []
    for i, q in enumerate(qs, 1):
        qid = str(q.get("id", ""))
        q_cat = q.get("category", "")
        q_prio = q.get("priority", "")
        opts = q.get("options") or []
        # 判断来源：kb_ 前缀 = 走了KB链路未答才转人工
        src = "KB_UNANS→人工" if qid.startswith("kb_") else ("DIRECT_人工" if (q_cat in {"conflict","contradiction"} or "contradict" in q_cat) else "其他")
        line = f"   {i:>2}. {qid} [{q_prio}/{q_cat}] 来源={src}  options={'有('+str(len(opts))+'条)' if opts else '⚠️空'}"
        print(line)
        q_text = str(q.get("question", "")).replace("\n"," ")[:120]
        print(f"        question: {q_text}")

        if qid.startswith("kb_"): kb_questions.append(q)
        else: direct_questions.append(q)

    print("\n" + "-"*90)
    print("🔍 关键断言检查:")
    # 检查 ref_undef 类（图片上传引用未定义）是否走了 KB 路径→转人工
    has_ref_undef_kb = any(("图片上传" in str(q.get("question","")) or "素材库" in str(q.get("question","")) or ("引用未定义" in str(q.get("context","")) and "图片" in str(q.get("context",""))))
                            and str(q.get("id","")).startswith("kb_") for q in qs)
    print(f"  ref_undef（图片上传引用未定义）走 KB 未答才转人工: {'✅ YES' if has_ref_undef_kb else '❌ NO（或KB已有历史定义已自动答了）'}")
    # 检查 contradiction 类（版本矛盾）走了 DIRECT 人工（没有kb_前缀）
    has_contradiction_direct = any(("版本" in str(q.get("question","")) and ("矛盾" in str(q.get("category","")) or "50" in str(q.get("question",""))))
                                    for q in direct_questions)
    print(f"  contradiction（版本50 vs 不设上限）直接人工（不走KB）: {'✅ YES' if has_contradiction_direct else '❌ NO'}")
    # 检查所有人工问题都有 options（建议答案）
    all_have_opts = all(len(q.get("options") or []) > 0 or (q.get("input_type") or "") in {"text","text_area"} for q in qs)
    print(f"  所有人工问题都有建议答案options: {'✅ YES' if all_have_opts else '⚠️ 有部分问题options为空'}")
    print(f"  KB路径转人工: {len(kb_questions)}条  直接人工: {len(direct_questions)}条")

async def main():
    await verify(REQ_ID, "目标需求8899（含版本矛盾+图片上传引用未定义）")
    await verify(HEALTHY_ID, "健康需求4184（DevBlog）验证零误报")

asyncio.run(main())
