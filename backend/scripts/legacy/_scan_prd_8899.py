"""详细扫描REQ-8899 PRD 所有验收标准 & 数据约束 & 业务规则：找「不设上限」在哪写的"""
import sys, asyncio, json, re
sys.path.insert(0, '/app')
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select

REQ_ID = "REQ-1786071385229-8899"

def find_key_deep(o, keywords, path=""):
    """深度递归搜索：找含任意关键词的字段路径 + 值"""
    results = []
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}" if path else k
            if isinstance(k, str) and any(kw in k for kw in keywords):
                s = json.dumps(v, ensure_ascii=False) if isinstance(v,(dict,list)) else str(v)
                results.append((p, s[:500]))
            results.extend(find_key_deep(v, keywords, p))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            p = f"{path}[{i}]"
            results.extend(find_key_deep(v, keywords, p))
    elif isinstance(o, str):
        if any(kw in o for kw in keywords):
            results.append((path, o[:500]))
    return results

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one()
        srr = r.srr_json or {}
        prd = srr.get("prd", {})

        # ① 所有验收标准(acceptance_criteria/acceptance/验收标准)深度扫描
        print("=" * 70)
        print("【关键词1: 不设上限 / 无上限 / 无限 / 上限不限】")
        kw_02 = ["不设上限", "无上限", "无限", "上限不限", "不限制上限"]
        hits_02 = find_key_deep(srr, kw_02)
        if hits_02:
            for p, v in hits_02:
                print(f"  ✅ 路径 {p[:80]}")
                print(f"    值: {v[:300]}\n")
        else:
            print("  ❌ 没找到直接关键词，启动模糊搜索")
            # 模糊: 包含"上限"但没有数字（即"无上限但不写'不设上限'四字"）
            all_text = json.dumps(srr, ensure_ascii=False, indent=2)
            for m in re.finditer(r"[^。\n；]{0,60}上限[^。\n；]{0,60}", all_text):
                context = m.group(0)
                if any(k in context for k in ["50", "10", "100"]):
                    print(f"  (有限上限) {context[:160]}")
                else:
                    print(f"  ⚠️  疑似无上限上下文: {context[:160]}")

        print("\n" + "=" * 70)
        print("【关键词2: 版本/version + 保留/数量/上限】")
        ver_contexts = find_key_deep(srr, ["版本保留", "保留数量", "保留上限", "保留多少", "数量上限"])
        if not ver_contexts:
            # 更宽松: 同时出现"版本"和"保留"或者"版本"和"50"
            all_text = json.dumps(srr, ensure_ascii=False)
            for m in re.finditer(r"[^。\n；]{0,80}(?:手动(?:标记)?版本|版本(?:控制|管理|保留|对比|数量|上限|限制)|历史版本|回滚版本)[^。\n；]{0,80}", all_text):
                print(f"  {m.group(0)[:200]}")
        else:
            for p, v in ver_contexts:
                print(f"  路径 {p[:80]} -> {v[:200]}")

        # ② 扫描: 引用未定义（沿用现有/与现有一致/按现有处理/上传规则）
        print("\n" + "=" * 70)
        print("【关键词3: 上传规则/沿用现有/与现有一致/参照现有/按现有】")
        refs = find_key_deep(srr, ["沿用现有", "与现有一致", "参照现有", "按现有", "遵循现有", "使用现有", "上传规则"])
        for p, v in refs:
            print(f"  ✅ 路径 {p[:80]}")
            print(f"    值: {v[:300]}\n")

        # ③ 详细列出所有功能点的 验收标准 + 数据约束
        print("\n" + "=" * 70)
        print("【所有功能点的 验收标准 + 数据约束 完整列表】")
        fps = prd.get("functional_points") or srr.get("functional_points") or srr.get("flows", {}).get("functional_points") or []
        if isinstance(fps, list):
            for i, fp in enumerate(fps, 1):
                name = fp.get("name") or fp.get("title") or f"fp-{i}"
                print(f"\n FP[{i}] {name}  {'='*max(1,50-len(name))}")
                # 验收标准
                for field in ["acceptance_criteria", "acceptance", "验收标准", "accept_criteria"]:
                    if fp.get(field):
                        val = fp[field]
                        s = "\n      - ".join(val) if isinstance(val, list) else str(val)[:400]
                        has_limit = any(k in s for k in ["50个", "不设上限", "上传规则", "沿用现有", "5%", "改动比例"])
                        marker = " ⭐⭐⭐" if has_limit else ""
                        print(f"  {field}:{marker}")
                        if isinstance(val, list):
                            for line in val:
                                marker2 = ""
                                if any(k in line for k in ["不设上限","50个","沿用现有","5%"]): marker2 = " ⭐⭐⭐⭐⭐⭐⭐⭐"
                                print(f"      - {line[:200]}{marker2}")
                        else:
                            print(f"      {s[:400]}")
                # 数据约束
                for field in ["data_constraints", "constraints", "数据约束", "业务规则", "rules"]:
                    if fp.get(field):
                        val = fp[field]
                        s = "\n      - ".join(val) if isinstance(val, list) else str(val)[:400]
                        has_limit = any(k in s for k in ["50个", "不设上限", "上传规则", "沿用现有", "5%", "改动比例"])
                        marker = " ⭐⭐⭐" if has_limit else ""
                        print(f"  {field}:{marker}")
                        if isinstance(val, list):
                            for line in val:
                                marker2 = ""
                                if any(k in line for k in ["不设上限","50个","沿用现有","5%"]): marker2 = " ⭐⭐⭐⭐⭐⭐⭐⭐"
                                print(f"      - {line[:200]}{marker2}")
                        else:
                            print(f"      {s[:400]}")
        else:
            print(f"  functional_points不是list！类型={type(fps)} 值前400字符={str(fps)[:400]}")
    finally:
        await session.close()

asyncio.run(main())
