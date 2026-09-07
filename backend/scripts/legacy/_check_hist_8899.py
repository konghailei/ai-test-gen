"""检查 REQ-8899 澄清历史：实际生成了哪些KB查询、哪些被KB回答了、上传规则问题去哪了"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select

REQ_ID = "REQ-1786071385229-8899"

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one_or_none()
        srr = r.srr_json or {}
        prd = srr.get("prd", {})

        # ========= 1. 澄清历史 =========
        history = srr.get("_clarify_history", []) or []
        print(f"【澄清历史】共 {len(history)} 轮")
        for i, round_ in enumerate(history, 1):
            print(f"\n\n{'=' * 70}")
            print(f"  第 {i} 轮 - 动作: {round_.get('action', '?')}  时间: {round_.get('timestamp', '?')}")
            print(f"  问题数: {len(round_.get('questions', []) or [])}  回答数: {len(round_.get('answers', {}) or {})}")
            qs = round_.get("questions", []) or []
            # 查看每轮questions里有没有 上传规则 或 ambiguity类别
            for q in qs[:40]:
                qid = str(q.get("id", ""))
                qtext = (q.get("question", "") or "").replace("\n", " ")
                qfield = q.get("field", "")
                qcat = q.get("category", "")
                qpri = q.get("priority", "")
                ans = q.get("answer", "") or ""
                resolved = q.get("resolved", "")
                ans_trunc = str(ans).replace("\n", " ")[:80]
                # 高亮含上传规则/5%/版本数量的
                marker = "  ⭐" if any(k in qtext for k in ["上传", "沿用", "5%", "50个", "不设", "版本"]) else ""
                print(f"    [{qcat:<12}/{qpri:<9}] field={qfield:<35}{marker}")
                print(f"      Q: {qtext[:120]}")
                if ans_trunc:
                    print(f"      A: {ans_trunc}  (resolved={resolved})")

            # 有没有KB查询的独立日志？
            kb_log = round_.get("kb_queries", []) or round_.get("kb_search", []) or round_.get("kb_check", [])
            if kb_log:
                print(f"\n  【KB查询日志】共 {len(kb_log)} 条")
                for kq in kb_log[:30]:
                    if isinstance(kq, dict):
                        q_txt = (kq.get("query") or kq.get("question") or str(kq))[:120]
                        hit = kq.get("answered") or kq.get("has_answer") or kq.get("found")
                        ans_ = kq.get("answer") or ""
                        if isinstance(ans_, str) and len(ans_) > 60:
                            ans_ = ans_[:60] + "..."
                        print(f"    {'✅命中' if hit else '❌未'} query={q_txt}  answer={ans_}")

        # ========= 2. 当前澄清状态：_clarify_state =========
        print(f"\n\n{'=' * 70}")
        print("  【当前澄清状态 _clarify_state】")
        cs = srr.get("_clarify_state", {})
        # 看是否有 _kb_cache / _kb_queries_answered 等缓存字段
        for key in sorted(cs.keys()):
            val = cs[key]
            if key in ("kb_answers_cache", "kb_queries_result", "baseline_context", "_kb_log", "_kb_query_results", "_kb_cache", "raw_kb_results", "_baseline"):
                if isinstance(val, list):
                    print(f"  {key}: (list, len={len(val)})")
                    for v in val[:10]:
                        if isinstance(v, dict):
                            q = (v.get("question") or v.get("query") or str(v))[:100]
                            a = (v.get("answer") or "")[:80]
                            ok = v.get("answered", "") or v.get("ok", "")
                            print(f"    - {'✅' if ok else '❌'} Q={q}")
                            if a: print(f"      A={a}")
                elif isinstance(val, dict):
                    print(f"  {key}: (dict, keys={list(val.keys())[:8]})")
                else:
                    print(f"  {key}: {str(val)[:200]}")
            else:
                preview = str(val)
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                print(f"  {key}: {preview}")
    finally:
        await session.close()

asyncio.run(main())
