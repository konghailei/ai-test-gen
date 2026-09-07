"""单独跑REQ-8899 clarify的4个关键子检测函数，诊断漏生成根因"""
import sys, asyncio, json, time
sys.path.insert(0, '/app')
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select
from app.agents.clarify_agent import clarify_agent

REQ_ID = "REQ-1786071385229-8899"

def print_qs(label, qs):
    print(f"\n{'='*70}\n【{label}】共 {len(qs)} 条")
    for i, q in enumerate(qs, 1):
        qtext = (q.get("question","") or "").replace("\n"," ")[:150]
        field = q.get("field","")[:40]
        cat = q.get("category","")
        pri = q.get("priority","")
        marker = ""
        if any(k in qtext for k in ["上传", "沿用", "现有图片", "素材库"]): marker += "  ⭐[上传规则]"
        if any(k in qtext for k in ["50个", "版本", "不设上限", "不设数量", "最多保留"]): marker += "  ⭐[版本上限]"
        if any(k in qtext for k in ["5%", "改动", "小修订"]): marker += "  ⭐[5%改动]"
        print(f"  [{i:2d}] {pri}/{cat:<11} field={field:<40}{marker}")
        print(f"        Q: {qtext}")

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one()
        srr = r.srr_json or {}
        product_id = srr.get("requirement", {}).get("product_id", r.product_id) or ""

        # 1. 引用未定义 + 描述不清 检测
        t = time.time()
        amb = clarify_agent._detect_ambiguity(srr)
        print_qs(f"1) _detect_ambiguity() 原生输出（含引用未定义）耗时{time.time()-t:.2f}s", amb)

        # 2. 跨文档矛盾 + 逻辑矛盾
        t = time.time()
        cross_conf = clarify_agent._detect_cross_doc_conflicts(srr)
        print_qs(f"2) _detect_cross_doc_conflicts() 耗时{time.time()-t:.2f}s", cross_conf)

        t = time.time()
        logic_conf = clarify_agent._detect_logical_contradictions(srr)
        print_qs(f"3) _detect_logical_contradictions() 耗时{time.time()-t:.2f}s", logic_conf)

        # 3. PRD专项检测 （OPT-A-01/02这类编号的验收标准引用未定义属于这里）
        t = time.time()
        prd_gaps, prd_issues = clarify_agent._detect_prd_specific_gaps(srr)
        print_qs(f"4a) _detect_prd_specific_gaps → gaps 耗时{time.time()-t:.2f}s", prd_gaps)
        print_qs(f"4b) _detect_prd_specific_gaps → issues", prd_issues)

        # 4. LLM语义生成的原始问题（_generate_questions）+ 必要性过滤前后
        print(f"\n{'='*70}\n【5】_generate_kb_queries + 必要性过滤")
        t = time.time()
        try:
            kb_raw = await clarify_agent._generate_kb_queries(srr, product_id)
            print(f"  5a) _generate_kb_queries() 原始 LLM 输出 {len(kb_raw)} 条 耗时{time.time()-t:.2f}s")
            for i, q in enumerate(kb_raw, 1):
                qtext = (q.get("question","") or "").replace("\n"," ")[:130]
                field = q.get("field","")[:40]
                cat = q.get("category","")
                marker = ""
                if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker += "  ⭐[上传规则]"
                if any(k in qtext for k in ["50个", "版本", "不设上限"]): marker += "  ⭐[版本上限]"
                if any(k in qtext for k in ["5%", "改动"]): marker += "  ⭐[5%改动]"
                print(f"    [{i:2d}] cat={cat:<11} field={field:<40}{marker}")
                print(f"        Q: {qtext}")
            kb_filtered = clarify_agent._filter_questions_by_necessity(kb_raw)
            removed = [q for q in kb_raw if q not in kb_filtered]
            if removed:
                print(f"\n  ⚠️  ⚠️  被必要性过滤 删除的 {len(removed)} 条：")
                for i, q in enumerate(removed, 1):
                    qtext = (q.get("question","") or "").replace("\n"," ")[:130]
                    field = q.get("field","")[:35]
                    cat = q.get("category","")
                    print(f"    [{i}] cat={cat} field={field}")
                    print(f"        Q: {qtext}")
        except Exception as e:
            import traceback
            print(f"  ❌ _generate_kb_queries 出错: {e}")
            traceback.print_exc()

        # 6. 最终 调用 clarify() 看完整问题列表
        print(f"\n{'='*70}\n【6】最终 clarify() 完整输出 与 澄清页面对比")
        try:
            t = time.time()
            from app.core.orchestrator import get_current_session_id
            result = await clarify_agent.clarify(srr, on_progress=None)
            final_qs = result.get("questions", []) or []
            print(f"  最终问题数: {len(final_qs)}  总耗时 {time.time()-t:.1f}s")
            for i, q in enumerate(final_qs, 1):
                qtext = (q.get("question","") or "").replace("\n"," ")[:140]
                field = q.get("field","")
                cat = q.get("category","")
                pri = q.get("priority","")
                ans = q.get("answer","")
                if isinstance(ans, str) and len(ans) > 80: ans = ans[:80] + "..."
                marker = ""
                if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker += "  ⭐[上传规则引用未定义]"
                if any(k in qtext for k in ["50个", "版本", "不设上限", "不设数量"]): marker += "  ⭐[50vs不设上限矛盾]"
                if any(k in qtext for k in ["5%", "改动", "小修订"]): marker += "  ⭐[5%改动判定]"
                print(f"  [{i:2d}] {pri}/{cat:<11} field={field[:35]:<35}{marker}")
                print(f"      Q: {qtext}")
                if ans: print(f"      A: {ans}")
            if result.get("kb_status"):
                print(f"  KB状态: {result['kb_status']}")
        except Exception as e:
            import traceback
            print(f"  ❌ clarify() 出错: {e}")
            traceback.print_exc()
    finally:
        await session.close()

asyncio.run(main())
