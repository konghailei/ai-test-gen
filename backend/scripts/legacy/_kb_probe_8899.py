"""对REQ-8899的三个预期问题做向量库直接检索（对比：LLM风格问题文本 vs 纯关键词），以及检查上传规则为什么没生成问题"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.milvus_store import vector_store
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select

PRODUCT_ID = None  # 不过滤产品，全局搜
TOP_K = 5

def print_hit(label, hits):
    print(f"\n【{label}】命中 {len(hits)} 条：")
    for i, h in enumerate(hits, 1):
        id_ = h.get("id", "")
        meta = h.get("metadata", {})
        title = h.get("title") or meta.get("title") or id_
        score = h.get("similarity") or h.get("score") or meta.get("score") or 0
        modules = meta.get("modules", "")
        snippet = (h.get("chunk_text") or h.get("text") or json.dumps(meta, ensure_ascii=False))[:100]
        print(f"  [{i}] score={score:.3f}  id={id_[:30]}  title={title[:50]}  modules={modules[:30]}")
        print(f"        snippet: {snippet}")

async def main():
    # ==== 测试1: 向量库搜 OPT-A-02 版本数量矛盾 ====
    queries_02 = [
        ("LLM风格长查询", "手动标记版本最多保留50个还是不设上限？历史版本对比手动标记数量冲突解决方案是什么？"),
        ("纯关键词短查询", "手动版本保留50个 不设上限 版本数量 矛盾 历史版本"),
        ("含冲突词", "手动标记版本 最多保留 50个 不设上限 跨节矛盾 冲突 不一致"),
    ]
    for label, q in queries_02:
        print_hit(f"OPT-A-02: {label}", vector_store.search_requirements(q, product_id=PRODUCT_ID, top_k=TOP_K))

    # ==== 测试2: 向量库搜 OPT-A-01 上传规则引用未定义 ====
    queries_01 = [
        ("LLM风格长查询", "图片格式与单文件大小沿用现有上传规则，具体的上传格式限制是什么？文件大小限制多少？"),
        ("纯关键词短查询", "图片 上传规则 文件大小 格式限制 沿用现有 规则 文档 格式 大小"),
        ("接近历史查询（实际生成的KB查询风格）", "现有图片上传规则的具体格式和单文件大小限制是什么？"),
    ]
    for label, q in queries_01:
        print_hit(f"OPT-A-01: {label}", vector_store.search_requirements(q, product_id=PRODUCT_ID, top_k=TOP_K))

    # ==== 测试3: 向量库搜 小修订5% 判定标准 ====
    queries_05 = [
        ("LLM风格长查询", "历史版本对比中，小修订不触发通知的正文改动比例（如5%）具体采用哪种计算方式？是按字符数还是段落数还是编辑距离？"),
        ("实际LLM生成的KB查询", "小修订不触发通知的正文改动5%具体如何计算？"),
        ("纯关键词短查询", "小修订 5% 正文改动 比例 计算 方式 字符数 段落数 编辑距离 单词数"),
        ("PRD原文中出现过的文本", "正文改动 < 5% 不向关注者发送更新通知 判定标准"),
    ]
    for label, q in queries_05:
        print_hit(f"OPT-5% : {label}", vector_store.search_requirements(q, product_id=PRODUCT_ID, top_k=TOP_K))

    # ==== 测试4: 检查所有包含"上传规则"字段的历史向量（看看是否真的存在定义）====
    print("\n" + "=" * 70)
    print("【向量库全局扫描：是否包含 图片上传/附件上传/素材库上传 规则的历史归档向量？】")
    has_rule_hits = vector_store.search_requirements(
        "图片上传规则 素材库上传 附件大小限制 格式限制 历史版本 规则定义", 
        product_id=PRODUCT_ID, top_k=10
    )
    for i, h in enumerate(has_rule_hits, 1):
        id_ = h.get("id", "")
        meta = h.get("metadata", {})
        title = h.get("title") or meta.get("title") or id_
        score = h.get("similarity") or h.get("score") or meta.get("score") or 0
        snippet = (h.get("chunk_text") or h.get("text") or json.dumps(meta, ensure_ascii=False))[:120]
        if score > 0.35:
            print(f"  ✅ 可能相关 [{i}] score={score:.3f}  title={title[:60]}")
            print(f"        snippet: {snippet}\n")

asyncio.run(main())
