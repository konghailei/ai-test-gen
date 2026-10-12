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
    """
    return {str(int(n)) for n in re.findall(r"\d+", str(text or ""))}


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


def is_near_duplicate(a: dict, b: dict) -> tuple[bool, str]:
    """判断两条用例是否近重复，返回 (是否重复, 依据说明)。

    数字口径不同 → 直接判为不重复（它们测的是不同边界值，不是同一场景）。
    """
    na, nb = _numbers(a.get("title")), _numbers(b.get("title"))
    if na != nb:
        return False, ""
    t = title_similarity(a, b)
    if t >= TITLE_SIM_THRESHOLD:
        return True, f"标题相似度={t:.2f}"
    s = step_similarity(a, b)
    if t >= TITLE_SIM_COMBINED and s >= STEP_SIM_COMBINED:
        return True, f"标题相似度={t:.2f} 且步骤相似度={s:.2f}"
    return False, ""


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
# ⚠️ v3.5.8（docs/27 E）：embedding 输入从「整例 JSON」切换为「语义字段
#   序列化」（_semantic_text，口径版本 e1）——元数据字段（priority/
#   gen_metadata/confidence/covered_point_ids 等）对"是否重复"零贡献，
#   却占 40-60% 文本量并加剧 bge 512 截断。切换后相似度分布必然漂移，
#   **0.92 阈值已安排 docs/27 C3 定标 gate 用 v1.0-r2 真实批次复标**
#   （三档对照：large×整例JSON / large×语义字段 / small×语义字段）。
SEMANTIC_SIM_THRESHOLD = 0.92

# 语义字段序列化口径版本（docs/27 E）。进向量缓存 key（B 方案）：
# 改口径必须递增版本号，否则旧缓存会以旧口径向量冒充新口径结果。
SEMANTIC_SERIALIZATION_VERSION = "e1"


def _semantic_text(case: dict) -> str:
    """去重专用序列化：只保留语义负载字段（口径 v=e1，docs/27 §2.2）。

    字段清单与取舍依据：
    - title：场景主信号（一级）；
    - type：用例类型（boundary/integration 等改写常带类型变化，弱信号保留）；
    - steps[].action / steps[].expected：操作与预期（一级，不含 step 序号）；
    - assertions：断言语义（dict 取 expected_value/target/description 值）。

    显式排除：priority / confidence / gen_metadata / covered_point_ids /
    source / id / status / review 相关字段——对重复判定零贡献；且 review
    节点会追加/修改它们，排除后 B 缓存才能跨 review 轮命中（E↔B 协同）。
    """
    parts: list[str] = [str(case.get("title") or ""), str(case.get("type") or "")]
    for s in case.get("steps") or []:
        if isinstance(s, dict):
            parts.append(f"{s.get('action', '')}->{s.get('expected', '')}")
        else:
            parts.append(str(s))
    for a in case.get("assertions") or []:
        if isinstance(a, dict):
            parts.append(" ".join(
                str(v) for v in (a.get("expected_value"), a.get("target"), a.get("description"))
                if v
            ))
        else:
            parts.append(str(a))
    return "\n".join(p for p in parts if p)


def semantic_near_duplicate(
    a: dict, b: dict, sim: float | None = None
) -> tuple[bool, str]:
    """语义级近重复判定（阈值 0.92 + 数字护栏）。

    Args:
        a, b: 两条用例 dict。
        sim: 两例整例 JSON embedding 的余弦相似度（0~1）。None 时视为无信号，
             直接判不重复（调用方拿不到 embedding 时安全兜底）。

    判定逻辑：
        1. 数字集合不同 → 不重复（与 is_near_duplicate 同一护栏：不同边界值
           不是同一场景，即使向量说很像）。
        2. sim < 0.92 → 不重复。
        3. 其余 → 重复（理由带相似度值，写入去重留痕）。
    """
    if sim is None:
        return False, ""
    na, nb = _numbers(a.get("title")), _numbers(b.get("title"))
    if na != nb:
        return False, ""
    if sim >= SEMANTIC_SIM_THRESHOLD:
        return True, f"语义相似度={sim:.4f}"
    return False, ""


def dedup_semantic(
    cases: list[dict],
    embed_func: "callable | None" = None,
    threshold: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """跨子轮语义去重（persist 前的安全网）。

    与 dedup_near_duplicates 同构：质量高者优先成为代表，被去重条目留痕
    （removed/kept/reason），输出保持原输入顺序。区别在判定信号——
    用语义字段 embedding 余弦（阈值 0.92）替代标题 3-gram。

    v3.5.8（docs/27 E）：embedding 输入为 _semantic_text 输出（口径 e1），
    不再整例 JSON 序列化。embed_func 收到的文本因此**不再是合法 JSON**，
    调用方/测试不得假设可 json.loads 解析（契约变更，见 test_case_dedup.py
    的 _scripted_embed 改造）。

    Args:
        cases: 待去重用例列表。
        embed_func: 文本→向量的函数（list[str] → list[vector]），
                    通常传 vector_store.embed_texts。None 时退化为纯
                    3-gram 判定（等价 dedup_near_duplicates），保证无
                    embedding 环境下行为安全。
        threshold: 覆盖默认阈值（定标/校准用）。

    Returns:
        (保留的用例列表, 被去掉的记录列表)
    """
    thr = threshold if threshold is not None else SEMANTIC_SIM_THRESHOLD
    indexed = [(i, c) for i, c in enumerate(cases or []) if isinstance(c, dict)]
    if not indexed:
        return list(cases or []), []
    if embed_func is None:
        # 无 embedding 信号 → 退化为既有 3-gram 去重（安全兜底，不丢功能）
        return dedup_near_duplicates(cases)

    from math import sqrt

    # v3.5.8（docs/27 E）：语义字段序列化（口径 e1）替代整例 JSON。
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
        vectors = embed_func(texts)
    except Exception:  # noqa: BLE001
        # embedding 失败 → 降级为 3-gram（不能因为向量化抖动击穿去重环节）
        return dedup_near_duplicates(cases)

    # 归一化余弦（embed_texts 产出可能未归一）
    def _norm_cos(va: list[float], vb: list[float]) -> float:
        na_ = sqrt(sum(x * x for x in va)) or 1.0
        nb_ = sqrt(sum(x * x for x in vb)) or 1.0
        return sum(x * y for x, y in zip(va, vb)) / (na_ * nb_)

    vec_by_id = {i: v for (i, _), v in zip(indexed, vectors)}
    kept: list[dict] = []
    kept_idx: list[int] = []
    removed: list[dict] = []
    for i, case in order:
        dup_of = None
        for j in kept_idx:
            sim = _norm_cos(vec_by_id[i], vec_by_id[j])
            dup, reason = semantic_near_duplicate(case, cases[j], sim=sim)
            if dup:
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


def dedup_near_duplicates(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """批内近重复去重。

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
            dup, reason = is_near_duplicate(case, rep)
            if dup:
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
