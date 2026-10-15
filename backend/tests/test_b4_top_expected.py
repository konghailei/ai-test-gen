"""B-4 顶层 expected（docs/32 §B-4，docs/31 F4：133/320 空缺）。

三件套：
1. 生成 prompt 顶层 expected 必填（静态文本契约）；
2. ReviewAgent 规则预检对顶层 expected 为空记 warning（软信号，不进 blocker）；
3. _calc_dimension_scores 的 completeness 口径拆 completeness_step /
   completeness_top 两键透出（消除"score 0.785 但 step 层零缺失"的口径盲区）。

全部确定性断言（不调 LLM、不连 DB）。
"""
from __future__ import annotations

from app.agents.review_agent import ReviewAgent
from app.prompts.gen_agent import UTCF_CASE_SCHEMA


def test_prompt_mandates_top_level_expected():
    """prompt 契约：schema 示例标注必填 + 输出规范含必填条目。"""
    assert "整体预期结果（必填" in UTCF_CASE_SCHEMA, \
        "UTCF schema 示例未标注顶层 expected 必填"
    from app.prompts import gen_agent as g
    blob = " ".join(v for v in vars(g).values() if isinstance(v, str))
    assert "顶层 expected 必填" in blob, "输出规范缺少顶层 expected 必填条目"


def test_rule_review_warns_on_missing_top_expected():
    """顶层 expected 为空 → warning（completeness 维），非 blocker；非空不报。"""
    agent = ReviewAgent()
    cases = [
        {"id": "TC-1", "title": "空预期", "steps": [
            {"step": 1, "action": "a", "expected": "e"}],
         "related_requirement": "REQ-1", "preconditions": ["p"]},
        {"id": "TC-2", "title": "有预期", "expected": "登录成功并跳转首页",
         "steps": [{"step": 1, "action": "a", "expected": "e"}],
         "related_requirement": "REQ-1", "preconditions": ["p"]},
    ]
    issues = agent._rule_based_review({"requirement": {"id": "REQ-1"}}, cases)
    top_issues = [
        i for i in issues
        if "顶层 expected" in str(i.get("message", ""))
    ]
    assert len(top_issues) == 1, f"应只对空 expected 的 TC-1 告警: {issues}"
    assert top_issues[0]["case_id"] == "TC-1"
    assert top_issues[0]["severity"] == "warning", "软信号不得升级为 blocker"
    assert top_issues[0]["dimension"] == "completeness"


def test_dimension_scores_split_completeness_layers():
    """completeness_step（step 级）与 completeness_top（顶层）分开透出。"""
    agent = ReviewAgent()
    cases = [
        # step 完整 + 顶层 expected 有
        {"id": "TC-1", "title": "t", "expected": "整体预期",
         "steps": [{"action": "a", "expected": "e"}], "assertions": []},
        # step 完整 + 顶层 expected 空（docs/31 主形态）
        {"id": "TC-2", "title": "t",
         "steps": [{"action": "a", "expected": "e"}], "assertions": []},
        # step 缺 expected + 顶层有
        {"id": "TC-3", "title": "t", "expected": "整体预期",
         "steps": [{"action": "a"}], "assertions": []},
    ]
    scores = agent._calc_dimension_scores(cases, [], point_coverage=None)
    comp = scores["completeness"]
    assert "completeness_step" in comp and "completeness_top" in comp
    # step 完整率 2/3（TC-3 缺 step expected）；顶层非空率 2/3（TC-2 空）
    # 容差 1e-3：app.utils.numbers.clamp01 保留 3 位小数（0.667），非全精度
    assert abs(comp["completeness_step"] - 2 / 3) < 1e-3, comp
    assert abs(comp["completeness_top"] - 2 / 3) < 1e-3, comp


def test_dimension_scores_layers_present_even_when_coverage_unknown():
    """coverage unknown 时两层口径仍透出（结构事实不受映射状态影响）。"""
    agent = ReviewAgent()
    cases = [{"id": "TC-1", "title": "t", "steps": [{"action": "a", "expected": "e"}]}]
    scores = agent._calc_dimension_scores(cases, [], point_coverage=None)
    comp = scores["completeness"]
    assert comp["status"] == "unknown"
    assert comp["completeness_step"] == 1.0
    assert comp["completeness_top"] == 0.0
