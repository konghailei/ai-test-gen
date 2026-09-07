"""debug 引用未定义：打印 REF_UNDEF_RE + concrete_defined_match 对 candidate_items 的逐行结果"""
import sys, asyncio, json, re
sys.path.insert(0, '/app')
from app.db.database import async_session
from app.db.models import Requirement

REQ_ID = "REQ-1786071385229-8899"

async def main():
    async with async_session() as sess:
        row = await sess.get(Requirement, REQ_ID)
    srr = row.srr_json if isinstance(row.srr_json, dict) else json.loads(row.srr_json)

    from app.agents.clarify_agent import ClarifyAgent
    agent = ClarifyAgent()

    # 直接跑 detect 并加 debug monkey patch
    import app.agents.clarify_agent as mod

    # 把 candidate_items 提取出来 + 原逻辑
    raw_text = srr.get("_raw_text", "") or ""
    shared_biz = srr.get("shared_business_rules", []) or []
    srr_biz = srr.get("business_rules", []) or []
    acceptance = srr.get("acceptance_criteria", []) or []
    fpl = srr.get("fp_list", []) or []
    fps = srr.get("fp_summary", []) or []

    candidate_items: list[tuple[str, str]] = []  # (text, source_tag)
    # 1. 结构化 acceptance_criteria
    for i, a in enumerate(acceptance[:200]):
        if isinstance(a, str):
            candidate_items.append((a.strip(), f"acceptance#{i}"))
        elif isinstance(a, dict):
            for k in ("content","criteria","description","name","expectation","expected"):
                if a.get(k) and isinstance(a[k], str):
                    candidate_items.append((a[k].strip(), f"acceptance#{i}.{k}"))
                    break
    # 2. 结构化 business_rules/shared_business_rules/fp_list
    for src, lst in (("shared_biz", shared_biz), ("srr_biz", srr_biz), ("fp_list", fpl),
                     ("fp_summary", fps)):
        for i, a in enumerate(lst[:200]):
            if isinstance(a, str):
                candidate_items.append((a.strip(), f"{src}#{i}"))
            elif isinstance(a, dict):
                t = " ".join(
                    f"{a.get(k,'')}"
                    for k in ("content","business_rule","description","name","title","summary","rule","expectation","expected")
                    if a.get(k) and isinstance(a[k], str)
                ).strip()
                if len(t) >= 4:
                    candidate_items.append((t, f"{src}#{i}"))
    # 3. _raw_text 中的 Markdown 列表条目（关键！）
    if raw_text:
        # 带 section 上下文的列表行
        current_section = "文档"
        last_md_heading = ""
        # 逐行处理
        lines = raw_text.splitlines()
        for idx, line in enumerate(lines):
            ls = line.rstrip()
            if not ls.strip():
                continue
            # Markdown heading: ## xxx
            h_m = re.match(r'^#{1,6}\s+(.+)$', ls)
            if h_m:
                last_md_heading = h_m.group(1).strip()[:30]
                current_section = last_md_heading or "文档"
                continue
            # Markdown 有序/无序列表（带或不带 [ ]  checkbox）
            m = re.match(r'^\s*(?:[-*]\s*(?:\[[ x]\]\s*)?|\d+\.\s*)\S', ls)
            if m:
                txt = ls[m.start():].strip()
                # 去掉 checkbox 前缀
                txt = re.sub(r'^[-*]\s*(?:\[[ x]\]\s*)?', '', txt).strip()
                if len(txt) >= 6:
                    tag = f"raw_text#{idx+1}[{current_section}]"
                    candidate_items.append((txt, tag))

    REF_UNDEF_RE = re.compile(
        r"沿\s*用\s*现\s*有|与\s*现\s*有.*(一\s*致|完\s*全\s*一\s*致|保\s*持\s*一\s*致)|"
        r"按\s*现\s*有.*(执\s*行|处\s*理|规\s*则)|"
        r"参\s*照|遵\s*循|基\s*于.*规\s*则|使\s*用.*现\s*有|采\s*用.*现\s*有|"
        r"与\s*(?:XX|上\s*述|既\s*有|原\s*系\s*统).*(一\s*致|规\s*则|处\s*理)"
    )

    THEME_DEFINITION_SIGNALS = [
        (
            {"素材库","上传规则","图片格式","图片大小","单文件大小","文件大小","现有图片上传","图片上传"},
            re.compile(
                r"((?:单\s*文\s*件|单\s*张|单\s*个|每\s*个\s*文\s*件|每\s*张|文\s*件\s*本\s*身)\s*(?:上\s*传|大\s*小|限\s*制)?\s*(?:最\s*大|不\s*超\s*过|≤|<|不\s*能\s*超\s*过)?\s*\d+\s*(?:MB|KB|M|K))"
                r"|((?:支\s*持|允\s*许|格\s*式|包\s*括)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|bmp)(?:[\s,，/、]|$))",
                re.IGNORECASE,
            ),
        ),
        (
            {"小修订","正文改动","< 5%","<5%","改动比例","5%","通知触发"},
            re.compile(r"按\s*(?:字\s*符\s*数|段\s*落\s*数|编\s*辑\s*距\s*离|单\s*词\s*数|字\s*数|行\s*数|字\s*节|行\s*数|字|字\s*节\s*数)|"
                       r"(?:字\s*符\s*数|段\s*落\s*数|编\s*辑\s*距\s*离|单\s*词\s*数|字\s*数|行\s*数)\s*(?:计\s*算|统\s*计|核\s*算|判\s*断|作\s*为\s*标\s*准|为\s*判\s*定|来\s*度\s*量|衡\s*量|度\s*量)"),
        ),
        (
            {"手动标记版本","保留数量","版本上限","自动保存版本"},
            re.compile(r"\d+\s*个|最\s*多\s*\d+|最\s*多\s*保\s*留|FIFO|LRU|按\s*(?:时\s*间|版\s*本|创\s*建)\s*顺\s*序|清\s*理\s*最\s*旧|自\s*动\s*删\s*除|保\s*留\s*策\s*略"),
        ),
        (
            {"格式","大小","限制","校验","规则","标准","规范"},
            re.compile(
                r"(\d+\s*(?:MB|KB|M|K|字\s*节|字|条|分\s*钟|小\s*时|天))"
                r"|(?:支\s*持|包\s*括|列\s*举)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|doc|docx|xlsx|xls|csv|mp4|mp3|zip|rar)",
                re.IGNORECASE,
            ),
        ),
    ]

    def _concrete_defined_match(txt, extra_ctx=""):
        for theme_kws, signal_re in THEME_DEFINITION_SIGNALS:
            text_for_theme = (txt + " " + (extra_ctx or ""))
            if not any(kw in text_for_theme for kw in theme_kws):
                continue
            m_txt = signal_re.search(txt)
            m_ctx = signal_re.search(extra_ctx) if extra_ctx else None
            hit_kws = [k for k in theme_kws if k in text_for_theme][:4]
            if m_txt or m_ctx:
                print(f"      ✅ signal matched! theme_hit={hit_kws} m_txt={repr(m_txt.group(0)[:30]) if m_txt else None} m_ctx={repr(m_ctx.group(0)[:30]) if m_ctx else None}")
                return True
            else:
                print(f"      ⚠️  theme_hit={hit_kws} 但 signal_re 无匹配 → 判定未定义")
        return False

    print(f"\n🔎 逐行检查引用未定义（candidate_items 共 {len(candidate_items)} 条，扫描每条看是否命中）")
    print("="*80)
    hits = 0
    for t, s in candidate_items:
        if not REF_UNDEF_RE.search(t):
            continue
        hits += 1
        print(f"\n[{s}] 原文: {t[:200]}")
        # extra_ctx
        extra_ctx = ""
        if raw_text:
            idx = raw_text.find(t[:20])
            if idx >= 0:
                extra_ctx = raw_text[max(0, idx-300): idx + len(t) + 600]
        self_defined = _concrete_defined_match(t)
        ctx_defined  = _concrete_defined_match("", extra_ctx) if extra_ctx else False
        print(f"   → REF_UNDEF_RE ✅匹配  self_defined={self_defined}  ctx_defined={ctx_defined}")
        if not (self_defined or ctx_defined):
            print(f"   🔴🔴🔴 应生成引用未定义问题！！")
    print(f"\n✅ 总共匹配 REF_UNDEF_RE 的候选条目数: {hits}")

asyncio.run(main())
