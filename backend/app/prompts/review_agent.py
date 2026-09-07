"""ReviewAgent 用例审核提示词。

对 GenAgent 生成的 UTCF 用例集进行 6 维度审核，生成审核报告和置信度评分。
遵循 Auto-Validation 框架理念：每个测试必须映射到具体需求（可追溯性）。

设计依据：docs/03-case-generation-flow.md §5.1（ReviewAgent 审核维度）
审核维度（6 项，docs/03 §5.1）：
    1. 完整性：覆盖所有 flows、boundary、business_rules
    2. 去重：语义重复用例
    3. 可执行性：前置条件 + 步骤 + 预期完整
    4. 一致性：用例间无矛盾
    5. 追溯性：每个用例有需求依据
    6. 覆盖率：相对 test_points_hint 的覆盖

字段语义统一（docs/03 §5.2）：
    - gen_confidence：GenAgent 生成时 LLM 自评估，仅作 ReviewAgent 输入，不对外暴露
    - final_confidence：ReviewAgent 多因子加权计算后的最终置信度，写入 UTCF.confidence
"""

from __future__ import annotations

import json

# ============================================================================
# 系统指令
# ============================================================================

SYSTEM_PROMPT = """你是一位严谨的测试用例审核专家。请对生成的测试用例集进行多维度质量审核，输出审核报告和改进建议。

## 审核原则

### 1. 基于证据审核（Evidence-based review）
- 每个问题必须能追溯到具体用例 ID 和 SRR 描述
- 没有明确证据支撑的"感觉"不如不报——宁可漏审一个模糊点，不报一个无证据的问题
- 审核结论必须包含 direct_quote 字段（引用 SRR 或用例中的原文）

### 2. 区分"缺陷"和"偏好"（Bug vs Preference）
- blocker/warning 仅用于客观缺陷（缺失、矛盾、不可执行）
- 风格偏好、命名习惯等不应作为 issue 上报
- info 级别用于非阻塞性建议

### 3. 不编造问题（Don't fabricate issues）
- 如果用例集整体质量良好，接受并输出空 issues 列表
- 不要为了"看起来审查得仔细"而放大微小问题

## 审核维度（6 项）

1. 完整性 — 是否覆盖所有 flows（happy_path + alternate_flows）、boundary_conditions、business_rules
2. 去重 — 是否存在语义重复用例（相同测试点不同表述）
3. 可执行性 — 前置条件 + 步骤 + 预期结果是否完整可执行
4. 一致性 — 用例间是否存在矛盾
5. 追溯性 — 每个用例是否有关联的 related_requirement
6. 覆盖率 — 相对 SRR.test_points_hint 的覆盖情况

严重程度：
- blocker：阻断发布，必须修复
- warning：建议修复，不阻断
- info：提示性信息，不阻断

输出要求：issues 按 severity 降序，含 direct_quote 字段；should_supplement 表示是否需要补充生成"""


# ============================================================================
# 审核报告 Schema
# ============================================================================

REVIEW_REPORT_SCHEMA = """输出 JSON 必须符合以下结构：

{
  "total_cases": 0,
  "passed_cases": 0,
  "blocked_cases": 0,
  "issues": [
    {
      "case_id": "TC-XXX-XXXX-XXXX",
      "dimension": "completeness|deduplication|executability|consistency|traceability|coverage",
      "severity": "blocker|warning|info",
      "message": "问题描述",
      "suggestion": "修正建议"
    }
  ],
  "coverage_summary": {
    "functional": 0,
    "boundary": 0,
    "exception": 0,
    "security": 0,
    "performance": 0
  },
  "avg_confidence": 0.0,
  "should_supplement": false,
  "supplement_hints": [
    {"case_type": "boundary|exception|security", "reason": "补充原因"}
  ]
}"""


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(
    srr: dict,
    test_cases: list[dict],
    gen_confidences: list[float] | None = None,
) -> str:
    """构建 ReviewAgent 的用户指令。

    Args:
        srr: SRR JSON 字典（用于追溯性和覆盖率检查）
        test_cases: GenAgent 生成的 UTCF 用例列表
        gen_confidences: 各用例的 gen_confidence 列表（GenAgent 自评估），
            供 ReviewAgent 参考，但不直接作为 final_confidence

    Returns:
        str: 完整的用户指令

    Note:
        - gen_confidence 仅作 ReviewAgent 输入参考，final_confidence 由多因子加权计算
          （match_score×0.4 + completeness×0.3 + consistency×0.2 + gen_self×0.1 + pattern_bonus）
        - 覆盖率检查基于 SRR.test_points_hint，未覆盖的测试点列入 supplement_hints
    """
    confidence_hint = ""
    if gen_confidences:
        confidence_hint = f"""
【各用例的 GenAgent 自评估置信度（仅参考，不作最终值）】
{gen_confidences}"""

    # v5.0 审核 prompt 降载：不再注入全量 SRR 与全量用例的完整 JSON（会导致
    # 大规模用例集时 prompt 可达 200K+，LLM 首 token 漫长、易超时/卡死）。
    # 覆盖核算已由 calc_point_coverage 确定性完成，这里仅保留供 6 维语义判断
    # 所需的紧凑需求概要 + 精简用例条目，审核颗粒度不变、prompt 体积骤降。
    _req = srr.get("requirement", {}) if isinstance(srr, dict) else {}
    _srr_compact = {
        "title": _req.get("title", ""),
        "summary": _req.get("summary", "") or _req.get("title", ""),
        "modules": _req.get("modules", []) or [],
        "business_rules_snippet": (str(srr.get("business_rules", ""))[:600] if isinstance(srr, dict) else ""),
    }
    _compact_cases = []
    for _c in test_cases:
        _steps = _c.get("steps", []) or []
        _asserts = _c.get("assertions", []) or []
        _compact_cases.append(
            {
                "id": _c.get("id", ""),
                "title": _c.get("title", ""),
                "type": _c.get("type", ""),
                "priority": _c.get("priority", ""),
                "related_fp": _c.get("related_fp", ""),
                "steps": len(_steps),
                "expected": (str(_c.get("expected", "")) or "").strip()[:160],
                "assertions": len(_asserts),
            }
        )

    return f"""请对以下测试用例集进行 6 维度质量审核。

{REVIEW_REPORT_SCHEMA}
{confidence_hint}

【需求概要（用于追溯性/一致性判断）】
{json.dumps(_srr_compact, ensure_ascii=False, indent=2)}

【待审核的测试用例集（精简：标题/类型/优先级/覆盖功能点/步骤数/预期要点）】
{json.dumps(_compact_cases, ensure_ascii=False, indent=2)}

请直接输出审核报告 JSON，不要包含其他解释性文字。"""


# ============================================================================
# 置信度计算（多因子加权，非 LLM 调用）
# ============================================================================

def calculate_confidence(
    case: dict,
    match_results: list[dict] | None = None,
) -> float:
    """计算用例的最终置信度 final_confidence（多因子加权）。

    对应 docs/03 §5.2 的 calculate_confidence 函数。
    本函数为纯计算，不调用 LLM，供 ReviewAgent 直接使用。

    v5.1 权重重构（解决冷启动全部 reviewed 问题）：
    原公式 match_score 权重 0.4 过高，冷启动时 KB 无相似向量
    导致 match_score=0，拉低总分至 0.6 附近，全部落入 reviewed。
    新公式降低 match_score 权重、提高完整性/一致性/gen_self 权重，
    并在冷启动（match_score=0）时给予 quality_base 基础分，
    使高质量用例在首轮即可达到 0.70 approved 阈值。

        final_confidence = 0.20 * match_score
                         + 0.30 * completeness
                         + 0.25 * consistency
                         + 0.15 * gen_self
                         + pattern_bonus
                         + quality_base  (冷启动时 +0.10)

    Args:
        case: UTCF 用例字典
        match_results: KB 相似用例检索结果，每项含 {similarity}

    Returns:
        float: 最终置信度，钳制到 [0, 1]

    Note:
        - match_score：KB 检索的最大相似度，无则 0.0（冷启动）
        - completeness：用例字段完整度（步骤/预期/前置条件/断言）
        - consistency：用例内部一致性（步骤与预期是否匹配）
        - gen_self：GenAgent 自评估置信度
        - pattern_bonus：参考了测试模式则 +0.1
        - quality_base：冷启动（无 KB 匹配）时的基础分 +0.10，
          使完整性+一致性高的用例首轮即可 approved
    """
    # 1. match_score：KB 检索最大相似度
    match_score = 0.0
    has_kb_match = False
    if match_results:
        similarities = [m.get("similarity", 0.0) for m in match_results]
        match_score = max(similarities) if similarities else 0.0
        has_kb_match = match_score > 0.01

    # 2. completeness：字段完整度
    completeness = _calc_completeness(case)

    # 3. consistency：内部一致性
    consistency = _check_consistency(case)

    # 4. pattern_bonus：参考测试模式
    pattern_bonus = 0.1 if case.get("source_pattern") else 0.0

    # 5. gen_self：GenAgent 自评估
    gen_metadata = case.get("gen_metadata", {}) or {}
    gen_self = float(gen_metadata.get("gen_confidence", case.get("gen_confidence", 0.5)))

    # 6. quality_base：冷启动补偿
    # 无 KB 匹配（冷启动首轮）时，完整性≥0.83 且一致性≥0.8 的
    # 高质量用例给予基础分补偿，使其有机会达到 0.70 approved。
    quality_base = 0.0
    if not has_kb_match:
        if completeness >= 0.83 and consistency >= 0.8:
            quality_base = 0.10
        elif completeness >= 0.67 and consistency >= 0.7:
            quality_base = 0.05

    # 多因子加权
    final_confidence = (
        0.20 * match_score
        + 0.30 * completeness
        + 0.25 * consistency
        + 0.15 * gen_self
        + pattern_bonus
        + quality_base
    )
    return min(max(final_confidence, 0.0), 1.0)


def _calc_completeness(case: dict) -> float:
    """计算用例字段完整度。

    Args:
        case: UTCF 用例字典

    Returns:
        float: 完整度评分 0.0-1.0
    """
    score = 0.0
    total = 6.0
    if case.get("title"):
        score += 1
    if case.get("preconditions"):
        score += 1
    steps = case.get("steps", []) or []
    if steps and all(s.get("action") and s.get("expected") for s in steps):
        score += 1
    if case.get("expected") or steps:
        score += 1
    if case.get("assertions"):
        score += 1
    if case.get("related_requirement"):
        score += 1
    return score / total


def _check_consistency(case: dict) -> float:
    """检查用例内部一致性。

    Args:
        case: UTCF 用例字典

    Returns:
        float: 一致性评分 0.0-1.0
    """
    score = 1.0
    steps = case.get("steps", []) or []
    # 步骤序号连续性
    if steps:
        orders = [s.get("step", i + 1) for i, s in enumerate(steps)]
        expected_orders = list(range(1, len(steps) + 1))
        if orders != expected_orders:
            score -= 0.3
    # 步骤数与断言数合理性（无断言且步骤>3 扣分）
    if len(steps) > 3 and not case.get("assertions"):
        score -= 0.2
    return max(score, 0.0)
