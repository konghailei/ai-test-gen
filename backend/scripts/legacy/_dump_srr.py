"""Dump SRR keys and prd.acceptance_criteria structure"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.models import Requirement
import pprint

REQ_ID = "REQ-1786071385229-8899"

async def main():
    async with async_session() as sess:
        row = await sess.get(Requirement, REQ_ID)
    srr = row.srr_json if isinstance(row.srr_json, dict) else json.loads(row.srr_json)
    print("SRR Top-level keys:", list(srr.keys()))
    print()
    for k in ["requirement", "prd", "acceptance_criteria", "flows", "cross_fp_interactions"]:
        v = srr.get(k)
        if v is None:
            continue
        if k == "requirement":
            print("=== requirement section keys ===")
            print(list(v.keys()) if isinstance(v, dict) else type(v))
        elif k == "prd" or k == "cross_fp_interactions":
            print(f"\n=== {k} section keys ===")
            print(list(v.keys()) if isinstance(v, dict) else type(v))
            if isinstance(v, dict):
                for kk in ["acceptance_criteria", "Acceptance Criteria", "验收标准", "business_rules", "shared_business_rules", "global_constraints"]:
                    vv = v.get(kk)
                    if vv is not None:
                        print(f"  → {kk}: type={type(vv).__name__} len={len(vv) if hasattr(vv,'__len__') else '?'}")
    # find all 验收标准相关
    import re
    raw = json.dumps(srr, ensure_ascii=False)
    # Dump all occurrences of '最多保留 50' / '不设上限' / '沿用现有'
    for kw in ["50个", "不设数量上限", "沿用现有", "与现有图片上传规则", "5%", "正文改动"]:
        positions = [m.start() for m in re.finditer(re.escape(kw), raw)]
        for p in positions[:2]:
            ctx = raw[max(0,p-60):p+80]
            print(f"\n  🔎 [{kw}] pos={p}: ...{ctx}...")

asyncio.run(main())
