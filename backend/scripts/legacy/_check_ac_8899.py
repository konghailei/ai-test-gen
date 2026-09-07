"""看REQ-8899的acceptance_criteria具体条目（第几条有50，第几条不设上限）"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.models import Requirement

REQ_ID = "REQ-1786071385229-8899"

async def main():
    async with async_session() as sess:
        row = await sess.get(Requirement, REQ_ID)
        srr = row.srr_json if isinstance(row.srr_json, dict) else json.loads(row.srr_json)
    ac = srr.get("acceptance_criteria") or []
    print(f"总验收标准数: {len(ac)} 条")
    print("="*70)
    for i, a in enumerate(ac, 1):
        desc = a.get("description","") if isinstance(a, dict) else str(a)
        is_50 = "50" in desc
        is_no_limit = any(w in desc for w in ["不设上限","不设数量","无限制","长期保留不限","不限数量","没有上限","没有数量限制"])
        is_version = any(w in desc for w in ["版本","手动标记","保留","数量","上限"])
        print(f"[{i:2d}] {desc[:120]}")
        if is_50 or is_no_limit or (is_version and i>4):
            print(f"       🔍 → 50={is_50}  no_limit={is_no_limit}  version_keywords={is_version}")
    print()
    # 同时输出引用未定义相关
    print("引用未定义关键词扫描:")
    for i, a in enumerate(ac, 1):
        desc = a.get("description","") if isinstance(a, dict) else str(a)
        if any(w in desc for w in ["沿用","与现有","与XX一致","按现有","按既有","参照","遵循","与...一致","完全一致","保持一致"]):
            print(f"  [{i}] {desc[:130]}")

asyncio.run(main())
