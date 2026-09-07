"""最小化验证：直接调 ClarifyAgent._detect_logical_contradictions(srr)，看规则层能否命中 50vs不设上限 + 引用未定义"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.models import Requirement
from app.agents.clarify_agent import ClarifyAgent

REQ_ID = "REQ-1786071385229-8899"

async def main():
    async with async_session() as sess:
        row = await sess.get(Requirement, REQ_ID)
    srr = row.srr_json if isinstance(row.srr_json, dict) else json.loads(row.srr_json)
    agent = ClarifyAgent()
    qs = agent._detect_logical_contradictions(srr)
    print(f"_detect_logical_contradictions → 共 {len(qs)} 条")
    print("="*70)
    for q in qs:
        print(f"\n▶ {q.get('id')}: [{q.get('priority')}/{q.get('category')}] {q.get('question')[:200]}")
        ctx = str(q.get("context",""))
        print(f"  context: {ctx[:200]}")
        if q.get("options"):
            opts = q["options"]
            print(f"  options({len(opts)}): " + " / ".join(str(o)[:50] for o in opts))

    # 额外：打印 candidate_items 里的 theme=版本保留数量/上限 的条目，看看解析对不对
    print("\n\n" + "="*70)
    print("🔍 candidate_items 解析调试（仅带关键词的版本/上传条目）：")
    import re
    raw_text = srr.get("_raw_text", "") or ""
    fp_summary = srr.get("fp_summary",[]) or []
    hits = []
    for fp in fp_summary[:50]:
        if isinstance(fp, dict):
            t = " ".join(str(v) for v in fp.values() if isinstance(v,str))
            if any(w in t for w in ["版本","保留","50个","不设上限","上传","沿用"]):
                hits.append(("fp_summary "+str(fp.get('name','')), t[:200]))
    if raw_text:
        for lineno, line in enumerate(raw_text.splitlines(),1):
            if re.match(r'^\s*(?:[-*]\s*(?:\[[ x]\]\s*)?|\d+\.\s*)\S', line.rstrip()):
                if any(w in line for w in ["版本","保留","50个","不设上限","上传","沿用","一致","5%"]):
                    hits.append((f"raw_text#{lineno}", line.strip()[:200]))
    for src, txt in hits:
        print(f"  [{src}] {txt}")

asyncio.run(main())
