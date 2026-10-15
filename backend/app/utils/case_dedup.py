"""批内近重复识别（跨生成通道的安全网）。

背景（2026-09-15 端到端实测）：
    一次生成会跑多个通道（逐 FP 分片 / 全量兜底 / 跨 FP 集成 / 审核后补生成），
    每个通道各自去重，但**跨通道**只有"完全同名"的标题级去重能拦住。
    实测 56 条里有 22 条落在互相重复的簇里：
      例：「锁定期间尝试登录提示剩余锁定时间」vs「锁定期间尝试登录被拒绝并提示剩余锁定时间」
    两句只差几个字，场景完全相同，却都留了下来 —— 直接推高用例库冗余、稀释 few-shot 质量，
    也让审核报告堆出 5 条去重问题、最终把发布决策压到人工审核。

设计取舍（宁可不报，不误删）：
    - 只做"高相似"判定；不做传递闭包（避免 A≈B、B≈C 就把 A、C 也并掉）；
    - 每条候选用**已保留集合**里的代表逐对比较，判定与顺序无关（按质量降序处理）；
    - **数字口径护栏**：两条标题里的数字集合不同就不合并 —— 实测数据里
      「失败次数边界-第4次…」与「…-第5次…」（t=0.48/s=0.42）与
      「锁定时长边界-59秒」与「…-60秒」几乎逐字相同，但测的是**不同的边界值**，
      靠相似度无法区分，只有数字能。这是本次唯一能同时"抓到真重复、不误删边界"的护栏。
    - **不做跨批次历史去重**：与库里旧用例相似不等于冗余（迭代回归用例本来就该存在），
      历史侧仍只做标题级去重。

阈值标定（2026-09-15，用一次真实运行的 56 条用例两两比对，共 46 组相似对）：
    真重复对标定值：(0.74,0.61)(0.65,0.02)(0.60,0.23)(0.58,0.43)(0.48,0.27)
    必须保留的对照：(0.48,0.42)=4次/5次边界；(0.23,1.00)=5次 vs 4-5次边界（步骤几乎同）
                   (0.39,0.07)(0.18,0.37)=不同场景
    → 取 title≥0.55；或 title≥0.45 且 step≥0.25；或 step≥0.90 且 title≥0.30；
      三者都叠加"数字集合相同"。
"""
from __future__ import annotations

import re

from app.utils.logger import logger

# 标题 3-gram 相似度：≥0.55 视为同一场景（实测重复对的下界）
TITLE_SIM_THRESHOLD = 0.55
# 标题中等相似 + 步骤动作有一定重合（步骤只作**辅助**信号）
TITLE_SIM_COMBINED = 0.45
STEP_SIM_COMBINED = 0.25
# 说明：曾试过"步骤≥0.90 且标题≥0.30 即合并"，但用真实 56 条标定时该规则
# **收益为 0**（6 条真重复全部由标题条件命中），却有实打实的误删风险——
# 不同场景若用了同样的样板步骤就会被并掉。故不采用。


def _numbers(text: object) -> set[str]:
    """标题里的数字集合（覆盖次数/时长等口径）。

    v3.4.1 修复 L-9：先去前导零再比较。原实现下「第04次」与「第4次」的数字集合分别是
    {"04"} / {"4"}，数字护栏会直接短路判定为不同，近重复永远合并不掉。
    注意只归一前导零，不改变数字大小语义（"4" 与 "5" 仍视为不同边界值）。

    v3.5.10（docs/32 B-6 第 3 件）：正则 \\d+ → \\d+(?:\\.\\d+)? —— 原实现把
    「9.9MB」吃成 {"9"}，「9.9」与「9.5」的差异被吞掉、护栏失效（docs/29
    实证误删形态）。整数去前导零、小数去尾随零（"9.90"→"9.9"，"04"→"4"）。
    """
    out: set[str] = set()
    for n in re.findall(r"\d+(?:\.\d+)?", str(text or "")):
        try:
            out.add(str(int(n)))
        except ValueError:  # 小数：float 归一去尾随零（"9.90" → "9.9"）
            out.add(str(float(n)))
    return out


def _norm(text: object) -> str:
    """归一化：去空白/标点、统一小写（中英混排都能比）。"""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _ngrams(text: str, n: int = 3) -> set[str]:
    s = _norm(text)
    if not s:
        return set()
    if len(s) < n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: dict, b: dict) -> float:
    return _jaccard(_ngrams(str(a.get("title", ""))), _ngrams(str(b.get("title", ""))))


def step_similarity(a: dict, b: dict) -> float:
    """步骤动作 4-gram 相似度（用动作文本，不含期望结果——场景相同但期望不同也算重复）。"""

    def sig(case: dict) -> set[str]:
        out: set[str] = set()
        for s in case.get("steps") or []:
            if isinstance(s, dict):
                out |= _ngrams(str(s.get("action", "")), 4)
        return out

    return _jaccard(sig(a), sig(b))


def quality_rank(case: dict) -> tuple:
    """保留谁：覆盖标注数 → 置信度 → 步骤数 → 断言数（越完整越该留）。

    标注数放第一位：重复的两条里，带覆盖标注的那条对覆盖率统计有价值。
    """
    from app.utils.coverage import extract_point_ids

    try:
        n_points = len(extract_point_ids(case))
    except Exception:  # noqa: BLE001
        n_points = 0
    return (
        n_points,
        float(case.get("confidence") or 0.0),
        len(case.get("steps") or []),
        len(case.get("assertions") or []),
    )


# ============================================================================
# 闸门1：结构护栏（v3.5.10 docs/32 B-6 分层闸门第 1 层）
# ============================================================================
# 背景（docs/29 语义去重误删 25-34% + 2026-10-14 角色变体探针）：
#   整例 JSON embedding 对"角色/实体/操作维度差异"不敏感——角色词在全文中
#   占比过小，向量分辨不出（探针实测角色变体 sim 0.95-0.99 vs 真重复 0.97，
#   犬牙交错，调阈值结构性失效）。只有文本层确定性比对拦得住。
#
# 设计原则（docs/32 §B-6）：确定性优先、宁可漏删不可误删。
#   护栏方向是**宁可误拦（多保留）不漏拦（误删合并）**——误拦的代价是
#   多几条重复用例（审阅台可见），漏拦的代价是角色覆盖永久缺口（不可逆）。
# 六维（任一命中即拦，不合并）：
#   ① 小数/数字口径 ② 角色词 ③ 实体词 ④ 操作词 ⑤ 否定/分支词 ⑥ 扩展名/单位
# 纯函数、零成本、可单测——能用确定性手段拦的绝不进 LLM（闸门2 只裁窄带）。

# 角色词表：静态兜底 + 调用方经 extra_role_words 注入 SRR user_roles 动态词
# （docs/32：SRR user_roles 动态收集 + 静态兜底词表）。
_ROLE_WORDS = (
    "超级管理员", "管理员", "普通用户", "只读用户", "只读成员", "访客", "游客",
    "匿名用户", "未登录用户", "注册用户", "作者", "审批人", "审核员", "审核人",
    "操作员", "运维", "客服", "运营", "会员", "租户", "子账号", "主账号",
    "admin", "guest", "author", "approver", "reviewer", "operator",
)

# 实体词表（产品/项目/模块是 docs/29 实证误删对；其余为常见域实体）。
_ENTITY_WORDS = (
    "产品", "项目", "模块", "订单", "商品", "文档", "需求", "知识库", "用例",
    "报表", "任务", "工单", "权限", "账号", "文件", "目录", "标签", "分类",
    "部门", "渠道", "库存", "支付", "退款", "菜单", "页面", "接口",
)

# 操作词族：同族=同义（创建≈新建，不拦），跨族=不同操作（创建 vs 编辑，拦）。
_OPERATION_GROUPS: dict[str, tuple[str, ...]] = {
    "create": ("创建", "新建", "新增", "添加", "建立", "注册"),
    "update": ("编辑", "修改", "更新", "调整", "变更"),
    "delete": ("删除", "移除", "清除", "作废", "注销"),
    "query": ("查询", "搜索", "查找", "检索", "筛选"),
    "export": ("导出", "下载"),
    "import": ("上传", "导入"),
    "login": ("登录", "登出", "退出登录"),
    "enable": ("启用", "激活"),
    "disable": ("禁用", "停用", "冻结", "锁定"),
    "submit": ("提交", "送审", "发起"),
    "approve": ("审批", "审核", "通过审批"),
    # ⚠️ 刻意不含"驳回/拒绝"：它们更多以**预期结果**形态单侧出现
    # （docs/29 实证真重复对「…提示剩余锁定时间」vs「…被拒绝并提示…」），
    # 放操作族会把这类真重复误拦（漏删）。其"相反分支"语义由下方
    # _BRANCH_PAIRS（通过/驳回、允许/拒绝）覆盖——只在正负两极
    # 分别出现在两例时才拦，同侧出现不拦。
}

# 否定/分支对照对（正向词, 负向词）：两例分别命中相反一极 → 拦。
_BRANCH_PAIRS = (
    ("通过", "未通过"), ("成功", "失败"), ("允许", "拒绝"), ("正常", "异常"),
    ("正确", "错误"), ("存在", "不存在"), ("可见", "不可见"), ("完整", "不完整"),
    ("支持", "不支持"), ("包含", "不包含"), ("开启", "关闭"), ("启用", "禁用"),
)

_EXT_RE = re.compile(r"\.[a-zA-Z][a-zA-Z0-9]{1,7}")
_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(kb|mb|gb|tb|b|ms|s|秒|分钟|小时|天|次|条|%|％)", re.I
)


def _case_text(case: dict) -> str:
    """护栏比对语料：title + description + preconditions + steps 全文本。

    steps 必须拼入——探针实证"同标题、角色只在步骤里"（登录身份不同）形态
    靠标题完全分辨不出，只能看步骤文本。
    """
    parts = [str(case.get(k) or "") for k in ("title", "description", "preconditions")]
    for s in case.get("steps") or []:
        if isinstance(s, dict):
            parts.append(str(s.get("action") or ""))
            parts.append(str(s.get("expected") or ""))
        else:
            parts.append(str(s))
    for a in case.get("assertions") or []:
        if isinstance(a, dict):
            parts.append(str(a.get("message") or ""))
    return "\n".join(parts)


def _branch_hit(text: str, word: str, opposite: str) -> bool:
    """分支词命中（先剔除反义长词再匹配，防"未通过"含"通过"的子串误判）。"""
    return word in text.replace(opposite, "")


def _word_set(text: str, words) -> set[str]:
    return {w for w in words if w in text}


def _op_groups(text: str) -> set[str]:
    return {
        g for g, ws in _OPERATION_GROUPS.items() if any(w in text for w in ws)
    }


def _structural_guards(
    a: dict, b: dict, extra_role_words: "set[str] | None" = None
) -> tuple[bool, str]:
    """结构护栏（闸门1，纯函数）：六维任一命中即拦，不合并。

    Returns:
        (True, "")：护栏全过，可进入后续判定（LLM 窄带/合并）。
        (False, "护栏拦截:<维度>…")：命中护栏，必须保留两条。

    防误删方向刻意取"宁可误拦"：误拦=多留一条重复（可见、可去），
    漏拦=角色/实体变体被合并（不可逆、覆盖永久缺口）。
    """
    ta, tb = _case_text(a), _case_text(b)

    # ① 数字/小数口径（标题级，与 is_near_duplicate 既有护栏同口径）
    na, nb = _numbers(a.get("title")), _numbers(b.get("title"))
    if na != nb:
        return False, f"护栏拦截:数字口径不同({sorted(na)} vs {sorted(nb)})"

    # ② 角色词集合（SRR 动态词 + 静态兜底）
    roles = set(_ROLE_WORDS) | set(extra_role_words or ())
    ra, rb = _word_set(ta, roles), _word_set(tb, roles)
    if ra != rb:
        return False, f"护栏拦截:角色词不同({sorted(ra)} vs {sorted(rb)})"

    # ③ 实体词集合
    #    v3.5.10 微调：一侧无实体词（∅ = 无主张）不拦——那是详略差异不是
    #    场景差异（docs/31 边界对「解析生成语义树节点数刚好400的HTML文件」
    #    vs「语义树节点数边界」实证），放行给闸门2 兜底。**两侧非空的不等
    #    一律拦**（含子集形态）：docs/29 #11 生产误删对就是 {文档} ⊂
    #    {需求,文档} 的子集形态，放宽子集会回归误删，维持"集合不等即拦"。
    ea, eb = _word_set(ta, _ENTITY_WORDS), _word_set(tb, _ENTITY_WORDS)
    if ea and eb and ea != eb:
        return False, f"护栏拦截:实体词不同({sorted(ea)} vs {sorted(eb)})"

    # ④ 操作词族（同族同义不拦：创建≈新建；跨族主操作不同拦：创建 vs 编辑）
    #    判定取「互不包含」而非「集合不等」：真重复对常单侧多提一个动作词
    #    （docs/29 定标对「…触发账号锁定」vs「…登录失败触发账号锁定」，
    #    集合不等会把这类真重复全部误拦、去重效力归零）。只有两侧各有
    #    对方没有的操作族（真正的跨族操作）才拦；子集关系视为同场景细节
    #    差异，放行给闸门2 LLM 窄带兜底。
    oa, ob = _op_groups(ta), _op_groups(tb)
    if oa and ob and not (oa <= ob or ob <= oa):
        return False, f"护栏拦截:操作词不同({sorted(oa)} vs {sorted(ob)})"

    # ⑤ 否定/分支词相反（同一对的正负两极分别出现在两例）
    for pos, neg in _BRANCH_PAIRS:
        pa, na_ = _branch_hit(ta, pos, neg), neg in ta
        pb, nb_ = _branch_hit(tb, pos, neg), neg in tb
        if (pa and nb_) or (na_ and pb):
            return False, f"护栏拦截:分支词相反({pos}/{neg})"

    # ⑥ 扩展名 / 单位
    #    v3.5.10 微调：与④操作词同构取「互不包含才拦」——真重复对常单侧
    #    多提单位/扩展名（docs/31 幂等对 [] vs ['次']、详述侧带单位），
    #    集合不等会误拦；一侧为空/子集 = 无主张或细节差异，放行给闸门2。
    #    真冲突（.docx vs .markdown、MB vs GB）两侧互不包含，照拦。
    xa = set(m.group(0).lower() for m in _EXT_RE.finditer(ta))
    xb = set(m.group(0).lower() for m in _EXT_RE.finditer(tb))
    if xa and xb and not (xa <= xb or xb <= xa):
        return False, f"护栏拦截:扩展名不同({sorted(xa)} vs {sorted(xb)})"
    ua = {m.group(1).lower() for m in _UNIT_RE.finditer(ta)}
    ub = {m.group(1).lower() for m in _UNIT_RE.finditer(tb)}
    if ua and ub and not (ua <= ub or ub <= ua):
        return False, f"护栏拦截:单位不同({sorted(ua)} vs {sorted(ub)})"

    return True, ""


def is_near_duplicate(
    a: dict, b: dict, extra_role_words: "set[str] | None" = None
) -> tuple[bool, str]:
    """判断两条用例是否近重复，返回 (是否重复, 依据说明)。

    判定链（v3.5.10 docs/32 B-6）：数字口径快检 → 相似度阈值 → 闸门1 结构护栏。
    护栏命中（角色/实体/操作/分支/扩展名/单位任一维不同）→ 判不重复，
    reason 携带命中维度（供留痕/探针验收，调用方仅在 dup=True 时消费 reason）。
    """
    na, nb = _numbers(a.get("title")), _numbers(b.get("title"))
    if na != nb:
        return False, ""
    t = title_similarity(a, b)
    s = None
    if t >= TITLE_SIM_THRESHOLD:
        s = -1.0  # 单标题条件达标，步骤不必再算
    elif t >= TITLE_SIM_COMBINED:
        s = step_similarity(a, b)
        if s < STEP_SIM_COMBINED:
            return False, ""
    else:
        return False, ""
    ok, guard_reason = _structural_guards(a, b, extra_role_words=extra_role_words)
    if not ok:
        return False, guard_reason
    if s == -1.0:
        return True, f"标题相似度={t:.2f}"
    return True, f"标题相似度={t:.2f} 且步骤相似度={s:.2f}"


# ============================================================================
# 跨子轮语义去重（缺陷 E，2026-09-22）
# ============================================================================
# 背景：四级去重与批内 3-gram 去重都在各自子轮/通道内生效，但**补生成合并**
# （merge_regression_cases）与跨通道改写（integration 通道重述 FP 分片场景）
# 产出的重复，标题表面相似度天然低——09-16 批次实测 10 对已知重复的
# 3-gram 相似度全部 < 0.48（阈值 0.55，命中 0/10），只能靠语义向量抓。
#
# 阈值定标（09-16 真实批次 21 条，系统口径 bge-large-zh + 整例 JSON）：
#   已知重复 10 对：0.8839 ~ 0.9787
#   必须保留 6 对：0.8393 ~ 0.9076（其中 0.9076 = 4/5边界 vs 第6次保持，
#                  数字护栏可救；0.8961 = 登录成功 vs 自动解锁，真不同场景）
#   → 取 0.92：命中 8/10，叠加数字护栏后误删 0；
#     漏掉的 2 对（0.8839/0.8927）留在审核环节人工把关
#     （与既有取舍一致：宁可不报，不误删）。
#
# ⚠️ v3.5.8（docs/27 C3 定标）：曾尝试把 embedding 输入从「整例 JSON」切到
#   「语义字段序列化」（口径 e1）——1055 条真实用例实测**否决**：
#   判重从 586 对跌到 119 对，漏判 469 对真重复（详见下方版本史）。
#   已回退整例 JSON（口径 e2），0.92 阈值继续有效（分布未变）。
SEMANTIC_SIM_THRESHOLD = 0.92

# v3.5.10 A 方案（2026-10-14 大哥拍板）：闸门2 窄带下探至 0.88。
# 实证依据（docs/31 320 条生产口径二跑回放）：4 处跨分片真重复的 6 对
# sim 落在 0.8988~0.9185（全部 <0.92 永远删不掉），而须保留边界对
# 0.8935 起与之犬牙交错——阈值手段在此结构性失效。处置：sim ∈
# [0.88, 0.92) 且护栏全过的对送闸门2 LLM 裁决，**默认保留，LLM 明确
# 确认"同一场景"才删**（不对称默认值：≥0.92 维持现状判定链；<0.88
# 不比较）。LLM 缺席/失败一律 fail-open 保留——窄带内误判的最坏后果
# 是"该删的没删"（回到现状），永远不会误删。
SEMANTIC_BAND_FLOOR = 0.88

# 语义字段序列化口径版本（docs/27 E）。进向量缓存 key（B 方案）：
# 改口径必须递增版本号，否则旧缓存会以旧口径向量冒充新口径结果。
#
# ⚠️ 版本史（定标实证，docs/27 C3，1055 条真实用例三档对照）：
#   e1 = 语义字段序列化（title+type+steps+assertions）——**已否决**。
#        T1 判重 119 对 vs T0（整例 JSON）586 对，漏判 469 对真重复
#        （T1 sim 中位 0.872，43/60 落在 0.90+，与须保留对分布重叠，
#        调阈值无法救回）。根因：整例 JSON 的键名/结构共现不是噪声，
#        而是该口径的工作部件——同场景用例的 JSON 结构 token 完全共现，
#        抬高了余弦基数，0.92 阈值（09-16 定标）就是在含该基数的分布
#        上定的。去掉结构 = 分布塌陷 = 判定失效。
#   e2 = 回退整例 JSON 序列化（与 C2 前逐字节一致：dumps 不排序）。
SEMANTIC_SERIALIZATION_VERSION = "e2"


def _semantic_text(case: dict) -> str:
    """去重序列化口径（当前 e2 = 整例 JSON，docs/27 C3 定标回退）。

    e1（语义字段：title+type+steps+assertions，排除元数据）经 1055 条
    真实用例定标**否决**——漏判 80%（详见上方版本史注释）。结论：
    "元数据/结构噪声"实际承担特征对齐职责，去重口径不得瘦身。

    本函数保留为唯一序列化入口：缓存 key 与后续口径实验都经由此处，
    将来若基于定标再启新口径，只改此处 + 递增版本号。
    """
    import json as _json

    return _json.dumps(case, ensure_ascii=False)


def semantic_near_duplicate(
    a: dict, b: dict, sim: float | None = None,
    extra_role_words: "set[str] | None" = None,
    threshold: float | None = None,
) -> tuple[bool, str]:
    """语义级近重复判定（阈值 0.92 + 数字护栏 + 闸门1 结构护栏）。

    Args:
        a, b: 两条用例 dict。
        sim: 两例整例 JSON embedding 的余弦相似度（0~1）。None 时视为无信号，
             直接判不重复（调用方拿不到 embedding 时安全兜底）。
        extra_role_words: SRR user_roles 动态角色词（叠加静态兜底词表）。
        threshold: 覆盖默认阈值（定标/校准用；v3.5.10 修复：原 dedup_semantic
            传入的 threshold 被本函数硬编码常量短路，从未生效）。

    判定逻辑（v3.5.10 docs/32 B-6 分层闸门）：
        1. sim 无信号 / < 阈值 → 不重复。
        2. 数字集合不同 → 不重复（不同边界值不是同一场景，即使向量说很像）。
        3. 闸门1 结构护栏（角色/实体/操作/分支/扩展名/单位）任一命中
           → 不重复，reason 携带命中维度。
        4. 全过 → 重复（理由带相似度值，写入去重留痕）。
        注：闸门2 LLM 窄带裁决在 dedup_semantic 循环里（只对"即将删除的对"
        触发），本函数保持纯确定性、可单测。
    """
    thr = threshold if threshold is not None else SEMANTIC_SIM_THRESHOLD
    if sim is None or sim < thr:
        return False, ""
    na, nb = _numbers(a.get("title")), _numbers(b.get("title"))
    if na != nb:
        return False, ""
    ok, guard_reason = _structural_guards(a, b, extra_role_words=extra_role_words)
    if not ok:
        return False, guard_reason
    return True, f"语义相似度={sim:.4f}"


# ============================================================================
# 进程内向量缓存（docs/27 B，v3.5.8）
# ============================================================================
# 背景：dedup_semantic 在主链路两个对称点全量重算——补生成合并点的输入
# 与主合并点 ~90% 重叠（v1.0-r2：第二轮 280 条里 266 条已算过），
# 缓存命中后第二轮只 embed 新增十几条（17min → <1min）。
#
# 设计要点：
# - **key 与文本解耦**（docs/27 C3 定标回退 E 后的关键修正）：
#   key = md5(model_id | 口径版本 | _cache_projection(case))，其中投影是
#   语义白名单（title/type/description/preconditions/tags/steps/assertions/
#   notes）；embedding 文本仍是整例 JSON（e2 口径）。原因：review() 会**原地
#   改写** case["confidence"]（review_agent.py:298，reviewed_cases 是同一批
#   dict 对象）——若 key 用整例 JSON，confidence 数值变化就会全量 miss，
#   跨轮缓存命中率归零（17min 问题复活）。投影排除 confidence 等易变字段，
#   语义内容不变即命中；title/steps 等语义字段变 → key 变 → 正确重算。
#   （注意与已否决的 e1 区分：e1 是拿白名单做 embedding **文本**——丢了
#   JSON 结构共现导致分布塌陷；此处白名单只做缓存 **key**，向量内容不变。）
# - 模型隔离：换模型（A）→ key 变 → 全量 miss（dedup_model_id() 已处理
#   加载失败回退的 global-fallback 标记）；口径版本递增同理。
# - 进程内 LRU（4096 条 × 1024 维 float ≈ 16MB）：两次去重同进程，
#   跨进程无需求，不引 Redis；进程重启缓存清空（只损失一轮，可接受）；
# - model_id 为空 = 模型未知 → **不缓存**（fail-safe：宁可不缓存也不错位）。
from collections import OrderedDict
import hashlib
import threading

_EMBED_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 4096
# v4.1（缺陷 C）：embed 分块大小。每块算完立即回填 LRU——崩溃只损失当前块，
# 且块边界给事件循环/其它推理任务让出交错窗口（不长期霸占 CPU）。
_DEDUP_EMBED_CHUNK = 32
_CACHE_LOCK = threading.Lock()

# 缓存投影白名单：语义内容字段（变 → 必须重算向量）。
# 排除：confidence（review 原地改写）/ id / gen_metadata / status /
# reviewed_by / source / weight / gen_batch_id 等易变或无语义字段。
_CACHE_PROJECTION_FIELDS = (
    "title", "type", "description", "preconditions", "tags",
    "steps", "assertions", "notes", "expected",
)


def _cache_projection(case: dict) -> str:
    """缓存 key 用的稳定投影（语义白名单，sort_keys 保序）。

    与 _semantic_text（embedding 文本，整例 JSON）刻意不同：
    投影只回答"语义内容变没变"，不参与 embedding。
    """
    import json as _json

    proj = {k: case.get(k) for k in _CACHE_PROJECTION_FIELDS}
    return _json.dumps(proj, ensure_ascii=False, sort_keys=True)


def _cache_key(model_id: str, projection: str) -> str:
    """缓存 key：模型 + 口径版本 + 语义投影 三元组绑定。"""
    payload = f"{model_id}|{SEMANTIC_SERIALIZATION_VERSION}|{projection}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _embed_cached(
    embed_func,
    model_id: str,
    texts: list[str],
    cache_keys: "list[str] | None" = None,
) -> list[list[float]]:
    """批量查缓存 → 只 embed miss 的 → 回填（保持输入顺序）。

    Args:
        embed_func: 文本→向量（输入 texts）。
        model_id: 生效模型标识（空 = 不缓存）。
        texts: embedding 文本（整例 JSON，口径 e2）。
        cache_keys: 与 texts 等长的缓存 key 依据（语义投影）；None 时
            退化为用 texts 自身做 key（测试/简单场景）。

    embed_func 返回数量与输入不符 → 抛错，由 dedup_semantic 的降级分支接住
    （与 embedding 抖动同一失败语义：降级 3-gram，不击穿）。
    """
    if not model_id or embed_func is None:
        return embed_func(texts) if embed_func is not None else []

    key_src = cache_keys if cache_keys is not None else texts
    keys = [_cache_key(model_id, k) for k in key_src]
    with _CACHE_LOCK:
        hit_map = {k: _EMBED_CACHE[k] for k in keys if k in _EMBED_CACHE}
        for k in keys:  # LRU touch：命中也刷新新鲜度
            if k in _EMBED_CACHE:
                _EMBED_CACHE.move_to_end(k)
    miss_idx = [i for i, k in enumerate(keys) if k not in hit_map]

    if not miss_idx:
        logger.debug("[DedupCache] 全命中: {}/{}", len(keys), len(keys))
        return [hit_map[k] for k in keys]

    # v4.1（缺陷 C）：分块 embed + 逐块回填。
    #   旧实现把全部 miss 一次性喂给 embed_func（526 条 ≈ 35 min 单次调用）：
    #     ① 调用期间缓存层零进展，崩溃即全部白算；
    #     ② 大列表 encode 内存峰值高；
    #     ③ 无法观测进度（进度条分块更新才可估时）。
    #   分块后每块算完立刻回填 LRU：崩溃只损失当前块，且与其他 to_thread
    #   任务（bge 检索/出箱投递）在 CPU 上更细粒度地交替，不长期霸占。
    #   与串行整批的结果**逐位一致**（同模型、同 normalize，仅拆分批边界）。
    miss_texts_all = [texts[i] for i in miss_idx]
    miss_vecs_all: list[list[float]] = []
    for start in range(0, len(miss_texts_all), _DEDUP_EMBED_CHUNK):
        block_texts = miss_texts_all[start:start + _DEDUP_EMBED_CHUNK]
        block_vecs = embed_func(block_texts)
        if len(block_vecs) != len(block_texts):
            raise RuntimeError(
                f"embed 返回数量不符: 请求 {len(block_texts)} 条，返回 {len(block_vecs)} 条"
            )
        miss_vecs_all.extend(block_vecs)
        # 逐块回填（崩溃只损失当前块；后续块命中也刷新新鲜度）
        with _CACHE_LOCK:
            base = start
            for j, v in enumerate(block_vecs):
                _EMBED_CACHE[keys[miss_idx[base + j]]] = v
            while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
                _EMBED_CACHE.popitem(last=False)

    miss_vecs = miss_vecs_all
    if len(miss_vecs) != len(miss_idx):
        raise RuntimeError(
            f"embed 返回数量不符: 请求 {len(miss_idx)} 条，返回 {len(miss_vecs)} 条"
        )
    logger.debug(
        "[DedupCache] 命中 {}/{}，新算 {}，缓存规模 {}",
        len(hit_map), len(keys), len(miss_idx), len(_EMBED_CACHE),
    )
    miss_map = {keys[i]: v for i, v in zip(miss_idx, miss_vecs)}
    return [hit_map[k] if k in hit_map else miss_map[k] for k in keys]


def count_cache_miss(cases: list[dict], model_id: str = "") -> int:
    """数一遍缓存 miss 条数（只查 key，不 embed）——进度估算用。

    v3.5.9：预热上线后主合并点 miss≈0，若仍按 len(cases)×单条耗时估算
    会严重虚高（显示"预计 70 分钟"实际 30 秒），观感像卡死。
    """
    if not model_id or not cases:
        return len(cases or [])
    keys = [_cache_key(model_id, _cache_projection(c))
            for c in cases if isinstance(c, dict)]
    with _CACHE_LOCK:
        return sum(1 for k in keys if k not in _EMBED_CACHE)


def prewarm_semantic_cache(
    cases: list[dict],
    embed_func: "callable | None" = None,
    model_id: str = "",
    chunk_size: int = 64,
) -> int:
    """预嵌入：提前把用例向量写入 B 缓存（docs/27 增量去重，v3.5.9）。

    设计：dedup_semantic 的耗时主体是 _embed_cached 里的 miss 部分（bge-large
    CPU ~3.8s/条）。生成是逐 FP 子轮推进的，每子轮完成后立即预嵌入本子轮
    用例，主合并点 dedup_semantic 时向量已全在缓存 → miss≈0，"全量重算"
    就地变成"增量计算"。判定逻辑零改动（向量同、判定同、结果同）。

    与 review() 的配合：review 会原地改写 case["confidence"]
    （review_agent.py:298），但缓存 key 是语义投影（排除 confidence），
    故 review 后的主合并点仍能命中生成期预嵌入的向量。

    Args:
        cases: 本子轮新产出的用例。
        embed_func / model_id: 与 dedup_semantic 同款（模型必须一致，否则
            key 不匹配等于白预热——调用方负责传同一来源）。
        chunk_size: 分块预嵌入（bge encode 长列表内存峰值高，64 条/块）。
    Returns:
        成功预嵌入的条数（失败/无 embed 环境返回 0，调用方无需检查——
        预热失败只是回退"主合并点现场算"的旧行为，不是错误）。
    """
    if embed_func is None or not model_id or not cases:
        return 0
    ok = 0
    try:
        for start in range(0, len(cases), chunk_size):
            block = [
                c for c in cases[start:start + chunk_size]
                if isinstance(c, dict)
            ]
            if not block:
                continue
            _embed_cached(
                embed_func, model_id,
                [_semantic_text(c) for c in block],
                cache_keys=[_cache_projection(c) for c in block],
            )
            ok += len(block)
    except Exception as e:  # noqa: BLE001
        # 预热是纯性能优化：任何失败（模型加载/内存/进程池）都不能影响
        # 生成主链路。dedup_semantic 自带 embed 失败 → 3-gram 降级兜底。
        logger.debug("[DedupCache] 预热失败（忽略，回退现场计算）: {}", str(e)[:150])
        return ok
    return ok


def dedup_semantic(
    cases: list[dict],
    embed_func: "callable | None" = None,
    threshold: float | None = None,
    model_id: str = "",
    llm_judge: "callable | None" = None,
    extra_role_words: "set[str] | None" = None,
    decision_log: "list | None" = None,
) -> tuple[list[dict], list[dict]]:
    """跨子轮语义去重（persist 前的安全网）。

    与 dedup_near_duplicates 同构：质量高者优先成为代表，被去重条目留痕
    （removed/kept/reason），输出保持原输入顺序。区别在判定信号——
    用整例 JSON embedding 余弦（阈值 0.92）替代标题 3-gram。

    v3.5.8（docs/27 C3）：序列化口径经 _semantic_text 统一出口。e1（语义
    字段瘦身）已定标否决并回退，当前 e2 = 整例 JSON（与 09-16 定标口径
    逐字节一致）。embed_func 收到的文本**是合法 JSON**（C2 期间的"不可
    解析"契约已随回退作废）。

    v3.5.8（docs/27 B）：向量经 _embed_cached 走进程内 LRU（model_id 绑定）。
    model_id 传 vector_store.dedup_model_id()（实际生效模型）；空 = 不缓存。

    v3.5.10（docs/32 B-6 分层闸门）：
        闸门1 结构护栏在 semantic_near_duplicate 内（纯确定性，六维任一命中
        即保留）；闸门2 LLM 窄带裁决经 llm_judge 注入——只对"护栏全过且
        sim≥0.92、即将删除的对"触发（每批通常 0-5 次），**fail-open 方向是
        保留**（LLM 失败/异常 → 当作"非重复"保留，宁可漏删不可误删）。
        None 时行为 = 只有闸门1（安全降级，单测环境不依赖 LLM）。
        v3.5.10 A 方案（大哥拍板）：窄带下探至 SEMANTIC_BAND_FLOOR=0.88——
        sim ∈ [0.88, 0.92) 且护栏全过的对也送闸门2，但**默认保留，LLM 明确
        确认才删**（与 ≥0.92 带的"LLM 否决才留"形成不对称默认值）；LLM 缺席
        （llm_judge=None）时该带一律保留。

    Args:
        cases: 待去重用例列表。
        embed_func: 文本→向量的函数（list[str] → list[vector]），
                    通常传 vector_store.embed_texts_for_dedup。None 时退化为纯
                    3-gram 判定（等价 dedup_near_duplicates），保证无
                    embedding 环境下行为安全。
        threshold: 覆盖默认阈值（定标/校准用）。
        model_id: 生效的 embed 模型标识（进缓存 key；空 = 模型未知，不缓存）。
        llm_judge: 闸门2 裁决 callable（签名 ``(a, b) -> bool``，True=是重复
            可合并）。只在"即将删除的对"上调用；返回 False/抛异常 → 保留。
        extra_role_words: SRR user_roles 动态角色词（闸门1 词表叠加）。
        decision_log: 可选审计输出列表。闸门1 拦截（reason 含命中维度）与
            闸门2 裁决（llm_distinct / llm_duplicate:sim）逐对 append 记录，
            供留痕落库（闸门3）消费；None 时不收集。

    Returns:
        (保留的用例列表, 被去掉的记录列表)
    """
    thr = threshold if threshold is not None else SEMANTIC_SIM_THRESHOLD
    indexed = [(i, c) for i, c in enumerate(cases or []) if isinstance(c, dict)]
    if not indexed:
        return list(cases or []), []
    if embed_func is None:
        # 无 embedding 信号 → 退化为既有 3-gram 去重（安全兜底，不丢功能）
        return dedup_near_duplicates(
            cases, extra_role_words=extra_role_words, decision_log=decision_log
        )

    from math import sqrt

    def _passes_guards(x: dict, y: dict) -> tuple[bool, str]:
        """数字护栏 + 闸门1 结构护栏全量判定（窄带路径专用）。

        semantic_near_duplicate 在 sim < thr 时早退、不跑护栏——窄带候选
        正落在该区间，需在送 LLM 前补判"护栏全过"。
        """
        nx, ny = _numbers(x.get("title")), _numbers(y.get("title"))
        if nx != ny:
            return False, f"护栏拦截:数字口径不同({sorted(nx)} vs {sorted(ny)})"
        return _structural_guards(x, y, extra_role_words=extra_role_words)

    # v3.5.8（docs/27 C3）：序列化经 _semantic_text 唯一出口（当前口径 e2
    #   = 整例 JSON；e1 语义字段瘦身已定标否决，见版本史注释）。
    texts = [_semantic_text(c) for _, c in indexed]
    # 代表选择排序：与 quality_rank 不同，**完整性优先于置信度**。
    # 定标实证（09-16 批次）：integration 改写版置信度常比 FP 分片版高
    # 0.002~0.005（LLM 噪声级），但步骤/断言更少——按 quality_rank 会留下
    # 改写版顶掉更完整的原版。代表选择要的是"最完整的留下"，
    # 置信度只当末位决胜。
    def _rep_rank(case: dict) -> tuple:
        return (
            len(case.get("covered_point_ids") or []),
            len(case.get("steps") or []),
            len(case.get("assertions") or []),
            float(case.get("confidence") or 0.0),
        )

    order = sorted(indexed, key=lambda pair: (_rep_rank(pair[1]), -pair[0]), reverse=True)
    try:
        # v3.5.8（docs/27 B）：进程内 LRU 缓存。key 用语义投影（_cache_projection，
        #   排除 confidence 等 review 会原地改写的字段），文本仍是整例 JSON。
        vectors = _embed_cached(
            embed_func, model_id, texts,
            cache_keys=[_cache_projection(c) for _, c in indexed],
        )
    except Exception:  # noqa: BLE001
        # embedding 失败（含缓存层抛出的数量不符）→ 降级为 3-gram
        # （不能因为向量化抖动击穿去重环节）
        return dedup_near_duplicates(
            cases, extra_role_words=extra_role_words, decision_log=decision_log
        )
    if len(vectors) != len(indexed):
        # 兜底：数量不符时 zip 会静默截断 → 下游 vec_by_id KeyError 击穿节点。
        # 与 embedding 抖动同一失败语义：降级 3-gram。
        logger.warning(
            "[DedupCache] embed 返回数量不符（请求 {} 返回 {}），降级 3-gram",
            len(indexed), len(vectors),
        )
        return dedup_near_duplicates(
            cases, extra_role_words=extra_role_words, decision_log=decision_log
        )

    # 归一化余弦（embed_texts 产出可能未归一）
    def _norm_cos(va: list[float], vb: list[float]) -> float:
        na_ = sqrt(sum(x * x for x in va)) or 1.0
        nb_ = sqrt(sum(x * x for x in vb)) or 1.0
        return sum(x * y for x, y in zip(va, vb)) / (na_ * nb_)

    # v4.1（缺陷 C）：O(n²) 余弦预计算为 numpy 矩阵点积。
    #   旧实现内层对每对候选调 _norm_cos（纯 Python 1024 维乘加）——
    #   500 条 kept × 500 候选 ≈ 25 万对 × 2×1024 次 Python 浮点 ≈ 数分钟。
    #   numpy 化后：先把全部向量 L2 归一化，任意两对余弦 = 一次点积，
    #   内层查表 O(1)。**数值逐位等价**（同向量、同归一化、同阈值判定）。
    vec_by_id = {i: v for (i, _), v in zip(indexed, vectors)}
    try:
        import numpy as _np

        _mat = _np.asarray([vec_by_id[i] for i, _ in indexed], dtype=_np.float32)
        _norms = _np.linalg.norm(_mat, axis=1, keepdims=True)
        _norms[_norms == 0] = 1.0
        _mat = _mat / _norms
        _row_of = {idx: k for k, (idx, _) in enumerate(indexed)}

        def _cos(i: int, j: int) -> float:
            return float(_mat[_row_of[i]] @ _mat[_row_of[j]])
    except Exception as _e:  # noqa: BLE001 - numpy 缺失/异常 → 回退纯 Python（行为不变）
        logger.debug("[DedupSemantic] numpy 加速不可用，回退纯 Python 余弦: {}", str(_e)[:120])
        _cos = _norm_cos  # type: ignore[assignment]

    kept: list[dict] = []
    kept_idx: list[int] = []
    removed: list[dict] = []

    def _log_decision(record: dict) -> None:
        if decision_log is not None:
            decision_log.append(record)

    for i, case in order:
        dup_of = None
        for j in kept_idx:
            sim = _cos(i, j)
            dup, reason = semantic_near_duplicate(
                case, cases[j], sim=sim, extra_role_words=extra_role_words,
                threshold=thr,
            )
            if not dup:
                # 闸门1 拦截留痕（reason 非空 = 护栏命中维度）
                if reason:
                    _log_decision({
                        "decision": "kept", "reason": reason,
                        "removed": case.get("id") or case.get("title", ""),
                        "removed_title": str(case.get("title", ""))[:120],
                        "kept": cases[j].get("id") or cases[j].get("title", ""),
                        "kept_title": str(cases[j].get("title", ""))[:120],
                    })
                    continue
                # ── A 窄带（v3.5.10，SEMANTIC_BAND_FLOOR）────────────────
                # sim ∈ [0.88, thr) 且护栏全过 → 闸门2 裁决，默认保留，
                # LLM 明确确认"同一场景"才删；LLM 缺席/失败 fail-open 保留。
                if llm_judge is None or not (SEMANTIC_BAND_FLOOR <= sim < thr):
                    continue
                ok_g, guard_reason = _passes_guards(case, cases[j])
                if not ok_g:
                    _log_decision({
                        "decision": "kept", "reason": guard_reason,
                        "removed": case.get("id") or case.get("title", ""),
                        "removed_title": str(case.get("title", ""))[:120],
                        "kept": cases[j].get("id") or cases[j].get("title", ""),
                        "kept_title": str(cases[j].get("title", ""))[:120],
                    })
                    continue
                try:
                    band_dup = bool(llm_judge(case, cases[j]))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[DedupGuard] 窄带裁决异常，fail-open 保留: {}", str(e)[:120]
                    )
                    band_dup = False
                if not band_dup:
                    _log_decision({
                        "decision": "kept", "reason": "llm_band_distinct",
                        "removed": case.get("id") or case.get("title", ""),
                        "removed_title": str(case.get("title", ""))[:120],
                        "kept": cases[j].get("id") or cases[j].get("title", ""),
                        "kept_title": str(cases[j].get("title", ""))[:120],
                    })
                    continue
                dup_of = (cases[j], f"llm_band_duplicate:语义相似度={sim:.4f}")
                break
            # 闸门2：LLM 窄带裁决（只对"即将删除的对"触发）。
            # fail-open：返回 False / 抛异常 / 超时 → 保留（宁可漏删不可误删）。
            if llm_judge is not None:
                try:
                    is_dup = bool(llm_judge(case, cases[j]))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[DedupGuard] LLM 裁决异常，fail-open 保留: {}", str(e)[:120]
                    )
                    is_dup = False
                if not is_dup:
                    _log_decision({
                        "decision": "kept", "reason": "llm_distinct",
                        "removed": case.get("id") or case.get("title", ""),
                        "removed_title": str(case.get("title", ""))[:120],
                        "kept": cases[j].get("id") or cases[j].get("title", ""),
                        "kept_title": str(cases[j].get("title", ""))[:120],
                    })
                    continue
                reason = f"llm_duplicate:{reason}"
            dup_of = (cases[j], reason)
            break
        if dup_of is None:
            kept.append(case)
            kept_idx.append(i)
        else:
            rep, reason = dup_of
            removed.append({
                "removed": case.get("id") or case.get("title", ""),
                "removed_title": str(case.get("title", ""))[:120],
                "kept": rep.get("id") or rep.get("title", ""),
                "kept_title": str(rep.get("title", ""))[:120],
                "reason": reason,
            })
    # 输出保持原输入顺序
    kept_ids = {id(c) for c in kept}
    ordered = [c for c in cases if isinstance(c, dict) and id(c) in kept_ids]
    return ordered, removed


def dedup_near_duplicates(
    cases: list[dict],
    extra_role_words: "set[str] | None" = None,
    decision_log: "list | None" = None,
) -> tuple[list[dict], list[dict]]:
    """批内近重复去重。

    v3.5.10（docs/32 B-6）：与 dedup_semantic 同型挂闸门1 结构护栏
    （is_near_duplicate 内），护栏拦截经 decision_log 留痕（reason 含命中维度）。

    Returns:
        (保留的用例列表, 被去掉的记录列表[{'removed','kept','reason'}])
    """
    indexed = [(i, c) for i, c in enumerate(cases or []) if isinstance(c, dict)]
    # 质量高者优先成为代表：先按质量降序，再按原顺序稳定排序，保证结果与输入顺序无关
    order = sorted(indexed, key=lambda pair: (quality_rank(pair[1]), -pair[0]), reverse=True)
    kept: list[dict] = []
    removed: list[dict] = []
    for _, case in order:
        dup_of = None
        for rep in kept:
            dup, reason = is_near_duplicate(case, rep, extra_role_words=extra_role_words)
            if not dup:
                if reason and decision_log is not None:
                    decision_log.append({
                        "decision": "kept", "reason": reason,
                        "removed": case.get("id") or case.get("title", ""),
                        "removed_title": str(case.get("title", ""))[:120],
                        "kept": rep.get("id") or rep.get("title", ""),
                        "kept_title": str(rep.get("title", ""))[:120],
                    })
                continue
            dup_of = (rep, reason)
            break
        if dup_of is None:
            kept.append(case)
        else:
            rep, reason = dup_of
            removed.append({
                "removed": case.get("id") or case.get("title", ""),
                "removed_title": str(case.get("title", ""))[:120],
                "kept": rep.get("id") or rep.get("title", ""),
                "kept_title": str(rep.get("title", ""))[:120],
                "reason": reason,
            })
    # 输出保持原输入顺序（前端/持久化对顺序有隐含依赖）
    kept_ids = {id(c) for c in kept}
    ordered = [c for c in cases if isinstance(c, dict) and id(c) in kept_ids]
    return ordered, removed
