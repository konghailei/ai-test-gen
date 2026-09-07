"""绕过Redis缓存直接调底层：Milvus向量原生 + PG BM25原生接口，看真实检索能力"""
import sys, asyncio, json, time
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.milvus_store import vector_store
from app.db.hybrid_search import HybridSearcher

PRODUCT_ID = "PROD-26475-996"
TOP_K = 8

async def direct_search(label, query, doc_type="requirement"):
    print(f"\n\n{'='*70}\n【{label}】 query={query[:80]}")
    # 1) 原生向量检索
    t0 = time.time()
    v_hits = vector_store.search_similar_requirements(query=query, product_id=PRODUCT_ID, top_k=TOP_K*2, status="archived", doc_type=doc_type)
    dt_v = time.time()-t0
    print(f"\n  🔹 Milvus向量 原生检索: {len(v_hits)}条 ({dt_v:.2f}s)")
    for i, h in enumerate(v_hits[:6], 1):
        sc = h.get("similarity",0)
        meta = h.get("metadata",{}) or {}
        title = meta.get("title") or (json.loads(meta["srr_json"]).get("requirement",{}).get("title","?") if meta.get("srr_json") else h.get("title",""))
        id_ = meta.get("req_id") or h.get("id","?")
        marker = " ✅" if sc >= 0.50 else ""
        print(f"  [{i}] score={sc:.3f}{marker}  id={id_[:30]}  title={str(title)[:60]}")
        if meta.get("srr_json"):
            try:
                srr = json.loads(meta["srr_json"])
                raw = json.dumps(srr.get("requirement",{})|srr.get("prd",{}), ensure_ascii=False)
                import re
                for kw in ["上传规则","大小限制","格式限制","沿用","5%","改动比例","50个","版本","不设上限","小修订","50","不设数量","保留"]:
                    m = re.search(r"[^。\n；]{0,25}%s[^。\n；]{0,50}" % re.escape(kw), raw)
                    if m:
                        print(f"        …{m.group(0)[:90]}…")
                        break
            except: pass

    # 2) 原生 BM25 检索
    async with async_session() as sess:
        t0 = time.time()
        hs = HybridSearcher()
        try:
            b_hits = await hs._bm25_search(sess, query=query, product_id=PRODUCT_ID, top_k=TOP_K*2, status="archived", modules=[])
            dt_b = time.time()-t0
            print(f"\n  🔸 PG BM25 原生检索（已修复JSONB）: {len(b_hits)}条 ({dt_b:.2f}s)")
            for i, h in enumerate(b_hits[:6], 1):
                sc = h.get("similarity",0)
                meta = h.get("metadata",{}) or {}
                title = h.get("document") or meta.get("title") or "?"
                id_ = meta.get("req_id") or h.get("id","?")
                marker = " ✅" if sc > 0 else ""
                print(f"  [{i}] rank={sc:.3f}{marker}  id={id_[:30]}  title={str(title)[:70]}")
        except Exception as e:
            import traceback
            print(f"\n  ❌ BM25 仍报错: {e}")
            traceback.print_exc()

async def main():
    print(f"Product={PRODUCT_ID}  绕过Redis缓存 直接查底层")
    cases = [
        ("上传规则-LLM长", "素材库的图片上传规则具体支持哪些格式？单文件大小限制多少MB？沿用现有图片上传规则的具体内容是什么？"),
        ("上传规则-原文", "与现有图片上传规则完全一致 图片格式 单文件大小 限制 素材库"),
        ("5%改动-LLM长", "文章小修订不触发通知的正文改动比例5%具体怎么计算？按字符数 段落数 编辑距离 单词数 判定"),
        ("版本上限50vs不设", "手动标记版本 最多保留 50个 超出清理  vs 长期保留 不设数量上限 验收标准 矛盾 冲突"),
    ]
    for lab, q in cases:
        await direct_search(lab, q)

asyncio.run(main())
