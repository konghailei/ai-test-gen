"""单独调试：引用未定义为什么没命中"""
import sys, asyncio, json, re
sys.path.insert(0, '/app')

# 直接贴实际两条文本
cases = [
    "素材库顶部展示已用容量/总容量；图片格式与单文件大小沿用现有上传规则，另新增个人总量配额 2GB",
    "素材库上传支持的图片格式、单文件大小限制及超限提示，与现有图片上传规则完全一致",
    "命令菜单与工具栏并存，现有工具栏保留不变（向下兼容）；命令项支持自定义分组，分组规则沿用系统菜单标准",
]

REF_UNDEF_VERBS = (
    r"沿\s*用\s*现\s*有|与\s*现\s*有.*(一\s*致|完\s*全\s*一\s*致|保\s*持\s*一\s*致)|"
    r"按\s*现\s*有.*(执\s*行|处\s*理|规\s*则)|"
    r"参\s*照|遵\s*循|基\s*于.*规\s*则|使\s*用.*现\s*有|采\s*用.*现\s*有|"
    r"与\s*(?:XX|上\s*述|既\s*有|原\s*系\s*统).*(一\s*致|规\s*则|处\s*理)"
)
REF_UNDEF_RE = re.compile(REF_UNDEF_VERBS)

THEME_DEFINITION_SIGNALS = [
    (
        {"素材库","上传规则","图片格式","图片大小","单文件大小","文件大小","现有图片上传","图片上传"},
        re.compile(r"(\d+\s*(?:MB|KB|M|K))|((?:支持|格式|包括)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|bmp)(?:[\s,，/、]|$))", re.IGNORECASE),
    ),
    (
        {"小修订","正文改动","< 5%","<5%","改动比例","5%","通知触发"},
        re.compile(r"按(?:字符数|段落数|编辑距离|单词数|字数|行数|字节)|字符数\s*计|编辑距|diff\s*距离|改动\s*字"),
    ),
    (
        {"手动标记版本","保留数量","版本上限","自动保存版本"},
        re.compile(r"\d+\s*个|最多\s*\d+|FIFO|LRU|按(?:时间|版本|创建)顺序|清理最旧|自动删除"),
    ),
    (
        {"格式","大小","限制","校验","规则","标准","规范"},
        re.compile(
            r"(\d+\s*(?:MB|KB|GB|M|K|G|字节|字|条|分钟|小时|天))"
            r"|(支持|包括|列举)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|doc|docx|xlsx|xls|csv|mp4|mp3|zip|rar)",
            re.IGNORECASE,
        ),
    ),
]

def concrete_defined_match(txt, extra_ctx=""):
    for theme_kws, sig_re in THEME_DEFINITION_SIGNALS:
        text_for = (txt + " " + (extra_ctx or ""))
        if not any(kw in text_for for kw in theme_kws):
            continue
        m1 = sig_re.search(txt)
        m2 = sig_re.search(extra_ctx) if extra_ctx else None
        print(f"  [theme-match] theme_kws[hit]={[k for k in theme_kws if k in text_for][:4]} sig_re_match_txt={bool(m1)} m1={m1.group(0)[:40] if m1 else None} sig_match_ctx={bool(m2)} m2={m2.group(0)[:40] if m2 else None}")
        if m1 or m2:
            return True
    return False

for case in cases:
    print(f"\n{'='*60}")
    print(f"CASE: {case[:120]}")
    print(f"  REF_UNDEF_RE.search? {bool(REF_UNDEF_RE.search(case))}")
    result = concrete_defined_match(case, "")
    print(f"  → concrete_defined_match(t) = {result}  ←  True=已定义(跳过), False=未定义(命中)")
    if not REF_UNDEF_RE.search(case):
        print("  ❌ REF_UNDEF_RE 没匹配到！检查pattern...")
        m = REF_UNDEF_RE.search(case)
        if not m:
            # 手工试子pattern
            for p in REF_UNDEF_VERBS.split("|")[:5]:
                sub = re.compile(p)
                mm = sub.search(case)
                print(f"     sub_pattern={p[:50]} → match={bool(mm)}  mm={mm.group(0) if mm else None}")
