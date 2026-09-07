"""直接查hybrid_searcher：模拟REQ-1465的图片上传query看KB里是否有"""
import asyncio, sys
sys.path.insert(0, '/app')
from app.db.hybrid_search import hybrid_searcher

QUERIES = [
    "图片素材库验收标准中引用现有图片上传规则，但文档未定义该规则的具体内容。请补充：支持的图片格式列表、单文件大小限制数值、超限提示文案",
    "图片上传规则 格式 大小 限制",
    "图片上传 支持格式 jpg png 单文件大小 5MB",
    "现有图片上传规则是什么",
    "素材库上传支持哪些图片格式大小限制",
]

async def main():
    product_id = "PROD-26475-996"
    for i, q in enumerate(QUERIES, 1):
        print(f"\n==== Query {i}: {q[:60]} ====")
        try:
            results = await hybrid_searcher.search(
                query=q,
                product_id=product_id,
                top_k=5,
            )
            print(f"  命中数: {len(results)}")
            for j, r in enumerate(results[:3], 1):
                score = r.get('score', 0)
                req = r.get('metadata', {})
                title = req.get('title','?')
                value = str(r.get('value',''))[:120].replace('\n',' ')
                field = r.get('field','?')
                print(f"  [{j}] score={score:.3f} req={title} field={field}")
                print(f"      value: {value}")
        except Exception as e:
            print(f"  ERROR: {e}")

asyncio.run(main())
