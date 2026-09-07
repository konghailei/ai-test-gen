"""直接调用 clarify_agent.clarify()，并用Monkey Patch记录所有中间阶段（LLM原始问题、必要性过滤前后、KB查询前后、MAX_KB_QUERIES截断前后、冲突分流）"""
import sys, asyncio, json, logging, time
sys.path.insert(0, '/app')
# 日志只开clarify_agent INFO以上
logging.basicConfig(level=logging.WARNING, format="%(levelname).1s %(name)s:L%(lineno)d | %(message)s")
logging.getLogger("app.agents.clarify_agent").setLevel(logging.INFO)
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select
from app.agents.clarify_agent import clarify_agent

REQ_ID = "REQ-1786071385229-8899"

# ===== Monkey Patch 记录中间状态 =====
_patches_applied = []
def patch_logger():
    from app.agents import clarify_agent as ca_mod
    # Patch 1: _generate_questions 前后
    orig_gen = ca_mod.ClarifyAgent._generate_questions
    async def patched_gen(self, srr, completeness, ambiguities, similar_reqs, augmentations, pg_context=None):
        print("\n" + "="*70)
        print("📝 【进入 _generate_questions(LLM语义检测)】")
        print(f"  前置规则检测: completeness important_missing={len(completeness.get('important_missing',[]))}")
        print(f"  前置规则检测: ambiguities={len(ambiguities)}条")
        print(f"  规则ambiguities详情:")
        for i, am in enumerate(ambiguities[:10], 1):
            print(f"    [{i}] field={am.get('field','')[:35]} desc={str(am.get('description','') or am.get('question',''))[:100]}")
        print()
        result = await orig_gen(self, srr, completeness, ambiguities, similar_reqs, augmentations, pg_context)
        qs = result.get("questions", []) or []
        print(f"  ✅ LLM返回questions: {len(qs)}条")
        for i, q in enumerate(qs, 1):
            qtext = (q.get("question","") or "").replace("\n"," ")[:140]
            cat = q.get("category","")
            pri = q.get("priority","")
            field = q.get("field","")[:40]
            marker = ""
            if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker += "  ⭐[上传规则引用未定义]"
            if any(k in qtext for k in ["50个", "版本", "不设上限", "最多保留", "不设数量"]): marker += "  ⭐[50vs不设上限矛盾]"
            if any(k in qtext for k in ["5%", "改动", "小修订", "判定标准"]): marker += "  ⭐[5%改动判定]"
            print(f"    [{i:2d}] {pri:<8}/{cat:<10} field={field:<38}{marker}")
            print(f"          Q: {qtext}")
        issues = result.get("issues_detected", []) or []
        if issues:
            print(f"  ⚠️  LLM还返回了issues_detected（共{len(issues)}），可能需要兜底转问题:")
            for i, iss in enumerate(issues[:10], 1):
                desc = (iss.get("description","") or "").replace("\n"," ")[:140]
                typ = iss.get("type","")
                sev = iss.get("severity","")
                print(f"    [{i}] {sev}/{typ}  {desc}")
        print("="*70)
        return result
    ca_mod.ClarifyAgent._generate_questions = patched_gen
    _patches_applied.append(("_generate_questions", orig_gen))

    # Patch 2: _query_kb_for_answers 前后 记录KB查询文本 + 命中情况
    orig_kb = ca_mod.ClarifyAgent._query_kb_for_answers
    async def patched_kb(self, srr, questions):
        print("\n" + "="*70)
        print(f"🔍 【进入 _query_kb_for_answers】共 {len(questions)} 个查询提问 KB:")
        for i, q in enumerate(questions, 1):
            qtext = (q.get("question","") or "").replace("\n"," ")[:130]
            field = q.get("field","")[:40]
            is_baseline = q.get("_is_baseline", False)
            marker = " [BASELINE]" if is_baseline else ""
            print(f"  [{i:2d}] {field:<40}{marker}")
            print(f"        查询文本: {qtext}")
        t0 = time.time()
        ans = await orig_kb(self, srr, questions)
        dt = time.time() - t0
        print(f"\n  KB 答案返回（耗时{dt:.1f}s）: {len(ans)}条，其中answered=True的 {sum(1 for a in ans if a.get('answered'))} 条")
        for i, a in enumerate(ans, 1):
            qtext = (a.get("question","") or "").replace("\n"," ")[:100]
            ok = a.get("answered")
            sources = a.get("sources", []) or []
            sims = [round(s.get("similarity",0),3) for s in sources[:3]]
            answ = (a.get("answer","") or "").replace("\n"," ")[:80]
            marker = ""
            if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker += "  ⭐[上传规则]"
            if any(k in qtext for k in ["50个", "版本", "上限"]): marker += "  ⭐[版本上限]"
            if any(k in qtext for k in ["5%", "改动", "小修订"]): marker += "  ⭐[5%改动]"
            print(f"  [{i:2d}] {'✅命中' if ok else '❌未'} sims={sims}  {qtext}{marker}")
            if answ and ok: print(f"        答案: {answ}")
        return ans
    ca_mod.ClarifyAgent._query_kb_for_answers = patched_kb
    _patches_applied.append(("_query_kb_for_answers", orig_kb))

patch_logger()

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one()
        srr = r.srr_json or {}
        print(f"🎯 需求: {r.id} / {r.title}")

        t0 = time.time()
        final_result = await clarify_agent.clarify(srr)
        dt = time.time() - t0

        print("\n" + "="*70)
        print(f"🏁 【最终澄清结果】总耗时 {dt:.1f}s  status={final_result.get('status')}  问题数={len(final_result.get('questions', []))}")
        if final_result.get("kb_status"):
            print(f"  KB 状态: {final_result['kb_status']}")
        print()
        qs = final_result.get("questions", []) or []
        for i, q in enumerate(qs, 1):
            qtext = (q.get("question","") or "").replace("\n"," ")[:140]
            field = q.get("field","")[:40]
            cat = q.get("category","")
            pri = q.get("priority","")
            ans = q.get("answer","")
            if isinstance(ans, str) and len(ans) > 80: ans = ans[:80] + "..."
            marker = ""
            if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker += "  ⭐[上传规则引用未定义]"
            if any(k in qtext for k in ["50个", "版本", "不设上限", "最多保留", "不设数量"]): marker += "  ⭐[50vs不设上限矛盾]"
            if any(k in qtext for k in ["5%", "改动", "小修订", "判定标准"]): marker += "  ⭐[5%改动判定]"
            print(f"  [{i:2d}] {pri:<8}/{cat:<10} field={field:<38}{marker}")
            print(f"      Q: {qtext}")
            if ans: print(f"      A: {ans}")
    finally:
        await session.close()

asyncio.run(main())
