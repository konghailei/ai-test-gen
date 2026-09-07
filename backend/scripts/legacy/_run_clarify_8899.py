"""对REQ-8899 跑一次完整澄清问题生成链路，看每一步输出（LLM问题、必要性过滤前后、KB查询前后）"""
import sys, asyncio, json, logging, time
sys.path.insert(0, '/app')

# 开DEBUG级日志
logging.getLogger("app.agents.clarify_agent").setLevel(logging.DEBUG)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s")

from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select
from app.agents.clarify_agent import ClarifyAgent

REQ_ID = "REQ-1786071385229-8899"

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one()
        srr = r.srr_json or {}
        product_id = srr.get("requirement", {}).get("product_id", r.product_id) or ""
        project_id = srr.get("requirement", {}).get("project_id", r.project_id) or ""
        print(f"=" * 80)
        print(f"  需求: {r.id}   product_id={product_id}  project_id={project_id}")
        print(f"=" * 80)

        agent = ClarifyAgent()

        # ---- 步骤1: 看 _generate_kb_queries 原始LLM输出 （包含KB查询问题）----
        print("\n\n【步骤1】_generate_kb_queries 原始输出（LLM生成的KB查询清单）")
        t0 = time.time()
        try:
            kb_qs_raw = await agent._generate_kb_queries(srr, product_id)
            print(f"  耗时: {time.time() - t0:.1f}s, 生成 {len(kb_qs_raw)} 条KB查询候选")
            for i, q in enumerate(kb_qs_raw, 1):
                qtext = (q.get("question", "") or "").replace("\n", " ")[:150]
                field = q.get("field", "")
                cat = q.get("category", "")
                print(f"  [{i:2d}] cat={cat:<10} field={field:<40}  Q={qtext}")
        except Exception as e:
            import traceback
            print(f"  ❌ _generate_kb_queries 出错: {e}")
            traceback.print_exc()

        # ---- 步骤2: 必要性过滤后 ----
        print("\n\n【步骤2】_filter_questions_by_necessity(KB查询候选) 之后：")
        filtered = agent._filter_questions_by_necessity(kb_qs_raw) if 'kb_qs_raw' in dir() else []
        print(f"  过滤后保留 {len(filtered)} 条")
        for i, q in enumerate(filtered, 1):
            qtext = (q.get("question", "") or "").replace("\n", " ")[:150]
            field = q.get("field", "")
            cat = q.get("category", "")
            print(f"  [{i:2d}] cat={cat:<10} field={field:<40}  Q={qtext}")

        # ---- 步骤3: 用hybrid_searcher 查真实向量库 看 沿用现有上传规则 是否有答案 ----
        print("\n\n【步骤3】直接用 3 个 LLM 风格问题 + 3 个关键词风格问题 查向量库")
        from app.db.hybrid_search import hybrid_searcher
        test_queries = [
            ("LLM风格长", "图片格式与单文件大小沿用现有上传规则，图片上传支持的具体格式有哪些？单文件大小限制是多少MB？素材库上传规则具体内容是什么？"),
            ("LLM风格短", "现有图片上传规则 具体格式 大小限制"),
            ("关键词短", "图片上传规则 格式 文件大小 限制 沿用现有"),
            ("LLM-5%比例", "小修订不触发通知 正文改动<5% 具体计算方式是按字符数/段落数/编辑距离还是单词数？"),
            ("关键词-5%", "小修订 5% 改动比例 计算 判定标准 字符数 段落 编辑距离"),
            ("关键词-50版本", "手动版本 保留 50个 上限 不设 版本数量 冲突"),
        ]
        for label, q in test_queries:
            try:
                t1 = time.time()
                hits = await hybrid_searcher.search_requirements(
                    query=q, top_k=5, product_id=product_id, modules=[],
                    status="archived", session=session, doc_type="requirement",
                )
                dt = time.time() - t1
                good = [h for h in hits if h.get("similarity", 0) >= 0.50]
                sims = [round(h.get("similarity",0),3) for h in hits]
                print(f"  [{label:<12}] hits={len(hits)}  sims={sims}  >=0.50的有{len(good)}条  耗时{dt:.2f}s")
                for gi, g in enumerate(good[:3], 1):
                    meta = g.get("metadata", {}) or {}
                    title = meta.get("title") or g.get("title") or meta.get("req_id", "")
                    score = g.get("similarity",0)
                    id_ = meta.get("req_id") or g.get("id","")
                    doc = (g.get("document", "") or "")[:80]
                    print(f"      #{gi} score={score:.3f} id={id_[:30]}  title={title[:50]}  doc={doc}")
            except Exception as e:
                import traceback
                print(f"  [{label:<12}] ❌ 失败: {e}")
                traceback.print_exc()

        # ---- 步骤4: 跑完整 clarify 流程，看最终生成的问题 ----
        print("\n\n【步骤4】完整 ClarifyAgent.run(SRR) 最终问题（与澄清页面里看到的一致）：")
        try:
            t2 = time.time()
            result = await agent.run(srr, product_id=product_id, project_id=project_id, requirement_id=REQ_ID, session=session)
            print(f"  完整耗时: {time.time()-t2:.1f}s")
            final_qs = result.get("questions", []) or []
            print(f"  最终问题数: {len(final_qs)}")
            for i, q in enumerate(final_qs, 1):
                qtext = (q.get("question","") or "").replace("\n", " ")[:140]
                field = q.get("field","")
                cat = q.get("category","")
                pri = q.get("priority","")
                ans = q.get("answer", "")
                if isinstance(ans, str) and len(ans) > 60: ans = ans[:60] + "..."
                ans_summary = f"  A={ans}" if ans else ""
                marker = ""
                if any(k in qtext for k in ["上传", "沿用", "现有图片"]): marker = " ⭐上传规则"
                if any(k in qtext for k in ["5%", "改动", "小修订"]): marker += " ⭐5%改动"
                if any(k in qtext for k in ["50个", "版本保留", "不设上限", "版本数量"]): marker += " ⭐版本上限"
                print(f"  [{i:2d}] {pri:<9}/{cat:<12} field={field:<35}{marker}")
                print(f"      Q={qtext}{ans_summary}")
        except Exception as e:
            import traceback
            print(f"  ❌ run() 出错: {e}")
            traceback.print_exc()
    finally:
        await session.close()

asyncio.run(main())
