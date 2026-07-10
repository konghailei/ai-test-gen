"""Gherkin 导出器。

将 UTCF 测试用例列表转换为 Gherkin Feature 文件，用于：
- BDD 场景化描述
- Cucumber / Behave 等框架执行
- 业务可读的验收标准

设计依据：docs/04-output-format.md §3.1（UTCF → Gherkin 转换）

输出结构：
    Feature: {related_requirement} {title}
      Scenario: {case_title}
        # Priority: P0 | Type: functional
        Given {precondition1}
        Given {precondition2}
        When {step1.action}
        Then {step1.expected}
        When {step2.action}
        Then {step2.expected}
"""

from __future__ import annotations

from app.utils.logger import logger


# ============================================================================
# 主导出函数
# ============================================================================

def export_gherkin(cases: list[dict]) -> str:
    """将用例列表导出为 Gherkin Feature 文件。

    Args:
        cases: 用例 JSON 列表，每项为单条 UTCF TestCase dict

    Returns:
        str: Gherkin .feature 格式文本

    Note:
        - 所有用例合并为同一个 Feature（按 related_requirement 聚合）
        - 若用例关联多个需求，Feature 标题使用 "测试用例集"
        - 每个用例转为一个 Scenario
        - 步骤转换规则（docs/04 §3.1）：
            * 前置条件 → Given
            * 第一个步骤（若无前置条件）→ Given，否则 → When
            * 步骤的 expected → Then
            * 后续步骤的 action → When
        - 字段缺失时跳过对应部分，不抛异常
    """
    if not cases:
        return "Feature: 测试用例集\n  # 无可导出的用例\n"

    lines: list[str] = []

    # ========================================================================
    # Feature 标题
    # ========================================================================
    req_ids = {c.get("related_requirement") for c in cases if c.get("related_requirement")}
    if len(req_ids) == 1:
        feature_title = f"Feature: {next(iter(req_ids))}"
    else:
        feature_title = "Feature: 测试用例集"
    lines.append(feature_title)
    lines.append(f"  # 共 {len(cases)} 个场景，导出于 AI-Test-Gen")
    lines.append("")

    # ========================================================================
    # 每个 case → 一个 Scenario
    # ========================================================================
    for case in cases:
        _append_scenario(lines, case)
        lines.append("")

    logger.debug("[Exporter/Gherkin] 生成完成: cases={}", len(cases))
    return "\n".join(lines)


# ============================================================================
# 单个用例的 Scenario 生成
# ============================================================================

def _append_scenario(lines: list[str], case: dict) -> None:
    """将单个用例追加为 Gherkin Scenario。

    Args:
        lines: 行列表（in-place 追加）
        case: 单条用例 dict

    Note:
        步骤关键词分配规则（docs/04 §3.1）：
        - 前置条件全部用 Given
        - 第一个步骤的 action：若无前置条件则用 Given，否则用 When
        - 后续步骤的 action：When
        - 每个步骤的 expected：Then
        - 使用 And 连接连续相同关键词，提升可读性
    """
    title = case.get("title", "（无标题）")
    priority = case.get("priority", "N/A")
    typ = case.get("type", "unknown")
    case_id = case.get("id", "")

    # Scenario 标题（含 case_id 便于追溯）
    scenario_title = title
    if case_id:
        scenario_title = f"[{case_id}] {title}"
    lines.append(f"  Scenario: {scenario_title}")
    lines.append(f"    # Priority: {priority} | Type: {typ}")

    # 前置条件 → Given
    preconditions = case.get("preconditions") or []
    for pc in preconditions:
        lines.append(f"    Given {pc}")

    # 步骤 → When/Then
    steps = case.get("steps") or []
    has_preconditions = bool(preconditions)

    for idx, step in enumerate(steps):
        action = step.get("action", "")
        expected = step.get("expected", "")

        # 第一个步骤的 action：无前置条件时用 Given，否则用 When（docs/04 §3.1）
        if idx == 0 and not has_preconditions:
            action_keyword = "Given"
        else:
            action_keyword = "When"

        lines.append(f"    {action_keyword} {action}")
        if expected:
            lines.append(f"    Then {expected}")

    # 整体预期（可选，追加为最后一个 Then）
    overall_expected = case.get("expected")
    if overall_expected and not steps:
        lines.append(f"    Then {overall_expected}")
