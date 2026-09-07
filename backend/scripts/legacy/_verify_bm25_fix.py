"""验证 BM25 修复 + 不用LLM直接查向量库，看历史归档中是否真的存在图片上传规则/5%改动比例/版本上限的定义"""
import sys, asyncio, json, time
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.hybrid_search import hybrid_searcher

PRODUCT_ID = "PROD-26475-996"
TOP_K = 8

async def search(label, query, doc_type="requirement"):
    async with async_session() as sess:
        t0 = time.time()
        hits = await hybrid_searcher.search_requirements(
            query=query, top_k=TOP_K, product_id=PRODUCT_ID,
            modules=[], status="archived", session=sess, doc_type=doc_type
        )
        dt = time.time()-t0
    print(f"\n【{label}】 query={query[:50]}  ({len(hits)}条, {dt:.2f}s)")
    good = [h for h in hits if h.get("similarity",0) >= 0.50]
    sims = [round(h.get("similarity",0),3) for h in hits]
    print(f"  all sims: {sims}  >=0.50: {len(good)}条")
    for i, h in enumerate(hits[:5],1):
        sc = h.get("similarity",0)
        meta = h.get("metadata",{}) or {}
        doc = h.get("document","") or ""
        # 如果没标题，从SRR里找
        title = meta.get("title") or (json.loads(meta["srr_json"]).get("requirement",{}).get("title","?") if meta.get("srr_json") else "")
        id_ = meta.get("req_id") or h.get("id","?")
        srr_js = meta.get("srr_json") or ""
        # 在srr文本中找关键词上下文
        kw_context = ""
        if srr_js:
            try:
                txt = json.dumps(json.loads(srr_js), ensure_ascii=False)
                import re
                for kw in ["上传规则","大小限制","格式限制","沿用","5%","改动比例","50个","版本","不设上限","小修订"]:
                    m = re.search(r"[^。\n；]{0,30}%s[^。\n；]{0,50}" % re.escape(kw), txt)
                    if m:
                        kw_context += f"  [{kw}]…{m.group(0)[:100]}…\n"
                        break
            except: pass
        marker = " ✅" if sc >= 0.50 else ""
        print(f"  [{i}] score={sc:.3f}{marker}  id={id_[:30]}  title={str(title)[:60]}")
        if doc: print(f"        doc: {doc[:80]}")
        if kw_context:
            print(f"        srr中的关键词上下文:\n{kw_context}")

async def main():
    print(f"Product={PRODUCT_ID}  BM25 JSONB修复后 搜索对比")
    # 不用LLM，直接用关键词+问句查向量库
    cases = [
        # 上传规则
        ("上传规则-LLM长问句", "素材库的图片上传规则具体支持哪些格式？单文件大小限制多少MB？沿用现有图片上传规则的具体内容是什么？"),
        ("上传规则-纯关键词", "图片 上传规则 文件大小 格式 限制 具体 素材库 现有 沿用"),
        ("上传规则-原文引用", "与现有图片上传规则完全一致 图片格式 单文件大小 限制"),
        # 5%改动比例
        ("5%改动-LLM问句", "文章小修订不触发通知的正文改动比例5%具体怎么计算？是按字符数 段落数 编辑距离 单词数 哪种方式判定？"),
        ("5%改动-关键词", "正文改动 5% 计算方式 字符 段落 编辑距离 小修订 判定标准"),
        # 版本上限
        ("版本上限-LLM问句", "手动标记版本 保留数量 最多50个 vs 不设数量上限 冲突 跨节矛盾 应该采用哪个方案？"),
        ("版本上限-关键词", "手动版本 标记 保留 50个 不设 数量 上限 历史版本 回滚"),
    ]
    for (lab, q) in cases:
        await search(lab, q)

asyncio.run(main())
