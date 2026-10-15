"""覆盖证据的离线回归测试；ID 对齐不证明业务语义或执行结果正确。"""

from copy import deepcopy

import pytest

from app.utils.coverage import (
    backfill_cases_point_ids,
    calc_point_coverage,
    extract_point_ids,
)


def _srr(count=3, priority="P1"):
    return {
        "requirement": {"priority": priority},
        "flows": {"happy_path": {"steps": [
            {"action": f"校验业务步骤{i}"} for i in range(1, count + 1)
        ]}},
    }


@pytest.mark.parametrize("field", [
    "cover_points", "covered_points", "covers_points", "covered_point_ids",
])
@pytest.mark.parametrize("value", [
    ["happy_path.1", "shared_rule.2"],
    "happy_path.1, shared_rule.2",
    ("happy_path.1", ["shared_rule.2"]),
])
def test_extract_structured_annotations_without_closure_error(field, value):
    assert extract_point_ids({field: value}) == {"happy_path.1", "shared_rule.2"}


@pytest.mark.parametrize("field", [
    "cover_points", "covered_points", "covers_points", "covered_point_ids",
])
def test_extract_structured_metadata_annotations(field):
    assert extract_point_ids({"gen_metadata": {field: ["happy_path.1"]}}) == {"happy_path.1"}


def test_extract_text_annotations_and_deduplicate():
    case = {
        "title": "验证 fp_login.rule.1",
        "notes": "覆盖: [happy_path.1], [fp_login.data.2]",
        "gen_metadata": {"notes": "[happy_path.1]"},
        "steps": [{"action": "检查 alternate_flow.2", "expected": "满足 shared_rule.1"}],
    }
    assert extract_point_ids(case) == {
        "fp_login.rule.1", "happy_path.1", "fp_login.data.2", "alternate_flow.2", "shared_rule.1",
    }


@pytest.mark.parametrize("cases", [[], [{"title": "没有需求要点"}], [{"notes": "[happy_path.1]"}]])
def test_zero_points_are_unknown_not_full_coverage(cases):
    result = calc_point_coverage({}, [], cases)
    assert result["coverage_status"] == "unknown"
    assert result["mapping_based"] is False
    assert result["total_points"] == result["covered_points"] == 0
    assert result["coverage_rate"] == result["high_priority_rate"] == 0.0
    assert isinstance(result["coverage_rate"], float)
    assert isinstance(result["high_priority_rate"], float)
    assert result["uncovered"] == []


def test_ten_duplicate_unannotated_cases_do_not_manufacture_coverage():
    cases = [{"title": "重复校验同一个功能"} for _ in range(10)]
    result = calc_point_coverage(_srr(), [], cases)
    assert result["case_count"] == 10
    assert result["coverage_status"] == "unknown"
    assert result["mapping_based"] is False
    assert result["coverage_rate"] == result["high_priority_rate"] == 0.0
    assert result["covered_points"] == result["high_priority_covered"] == 0
    assert {p["id"] for p in result["uncovered"]} == {"happy_path.1", "happy_path.2", "happy_path.3"}
    assert result["coverage_basis"] == "observed_lower_bound"
    assert result["observed_coverage_rate"] == 0.0
    assert result["estimated_coverage_rate"] is None


def test_invalid_ids_are_reported_but_never_counted_as_annotations():
    cases = [{"covered_point_ids": ["happy_path.999", "not-a-point"]} for _ in range(10)]
    result = calc_point_coverage(_srr(), [], cases)
    assert result["coverage_status"] == "unknown"
    assert result["mapping_based"] is False
    assert result["cases_with_valid_ids"] == 0
    assert result["annotation_rate"] == 0.0
    assert result["invalid_point_ids"] == ["happy_path.999", "not-a-point"]
    assert result["covered_points"] == 0
    assert result["coverage_rate"] == result["high_priority_rate"] == 0.0
    assert len(result["uncovered"]) == 3


def test_full_explicit_mapping_is_observed_not_estimated():
    cases = [{"covered_point_ids": [f"happy_path.{i}" for i in range(1, 4)]}]
    result = calc_point_coverage(_srr(), [], cases)
    assert result["coverage_status"] == "mapped"
    assert result["mapping_based"] is True
    assert result["coverage_rate"] == result["high_priority_rate"] == 1.0
    assert result["covered_points"] == result["high_priority_covered"] == 3
    assert result["covered_point_ids"] == ["happy_path.1", "happy_path.2", "happy_path.3"]
    assert result["observed_coverage_rate"] == 1.0
    assert result["estimated_coverage_rate"] is None
    assert result["uncovered"] == []
    assert result["invalid_point_ids"] == []


def test_partial_mapping_below_threshold_keeps_observed_lower_bound():
    cases = [{"notes": "[happy_path.1]"}] + [{"title": "无标注"} for _ in range(9)]
    result = calc_point_coverage(_srr(), [], cases)
    assert result["coverage_status"] == "unknown"
    assert result["mapping_based"] is False
    assert result["annotation_rate"] == 0.1
    assert result["cases_with_valid_ids"] == 1
    assert result["coverage_rate"] == result["high_priority_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert result["covered_points"] == result["high_priority_covered"] == 1
    assert {p["id"] for p in result["uncovered"]} == {"happy_path.2", "happy_path.3"}


def test_only_valid_annotations_count_toward_mapping_threshold():
    cases = ([{"notes": "[happy_path.1]"}] + [{"notes": "[happy_path.999]"} for _ in range(9)])
    result = calc_point_coverage(_srr(), [], cases)
    assert result["coverage_status"] == "unknown"
    assert result["annotation_rate"] == 0.1
    assert result["cases_with_valid_ids"] == 1
    assert result["invalid_point_ids"] == ["happy_path.999"]
    assert result["covered_points"] == 1


def test_threshold_does_not_turn_duplicate_ids_into_additional_coverage():
    cases = [{"notes": "[happy_path.1]"} for _ in range(3)] + [{"title": "无标注"} for _ in range(7)]
    result = calc_point_coverage(_srr(), [], cases)
    assert result["coverage_status"] == "mapped"
    assert result["annotation_rate"] == 0.3
    assert result["covered_points"] == 1
    assert result["coverage_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert len(result["uncovered"]) == 2


def test_high_priority_coverage_only_counts_matching_high_priority_ids():
    fps = [{"id": "fp_low", "priority": "P2", "srr_json": {"business_rules": ["低优先级规则"]}}]
    result = calc_point_coverage(_srr(2), fps, [{"notes": "[fp_low.rule.1]"}])
    assert result["coverage_status"] == "mapped"
    assert result["covered_points"] == 1
    assert result["high_priority_total"] == 2
    assert result["high_priority_covered"] == 0
    assert result["high_priority_rate"] == 0.0
    assert [p["id"] for p in result["uncovered"]] == ["happy_path.1", "happy_path.2"]


def test_no_high_priority_points_does_not_claim_full_high_priority_coverage():
    result = calc_point_coverage(_srr(1, "P2"), [], [{"notes": "[happy_path.1]"}])
    assert result["coverage_rate"] == 1.0
    assert result["high_priority_total"] == 0
    assert result["high_priority_rate"] == 0.0


def test_unknown_keeps_all_unmapped_points_without_truncation():
    result = calc_point_coverage(_srr(60), [], [])
    assert result["coverage_status"] == "unknown"
    assert len(result["uncovered"]) == 60


def test_heuristic_backfill_is_labelled_and_never_promoted_to_observed_mapping():
    case = {"title": "校验业务步骤1", "notes": "原始说明", "gen_metadata": {"notes": "模型备注"}}
    count = backfill_cases_point_ids([case], _srr(1), [])
    assert count == 1
    assert case["mapping_source"] == "heuristic"
    assert case["gen_metadata"]["mapping_source"] == "heuristic"
    assert case["notes"].startswith("原始说明")
    assert case["gen_metadata"]["notes"].startswith("模型备注")
    assert "[happy_path.1]" in case["notes"]
    assert extract_point_ids(case) == {"happy_path.1"}
    result = calc_point_coverage(_srr(1), [], [case])
    assert result["coverage_status"] == "unknown"
    assert result["mapping_based"] is False
    assert result["coverage_rate"] == result["high_priority_rate"] == 0.0
    assert result["covered_points"] == 0
    assert result["heuristic_point_ids"] == ["happy_path.1"]
    assert result["cases_with_valid_ids"] == 0
    assert result["uncovered"][0]["id"] == "happy_path.1"
    before = deepcopy(case)
    assert backfill_cases_point_ids([case], _srr(1), []) == 0
    assert case == before


@pytest.mark.parametrize("location", ["case", "gen_metadata"])
def test_heuristic_marker_survives_either_metadata_location(location):
    case = {"notes": "[happy_path.1]"}
    if location == "case":
        case["mapping_source"] = "heuristic"
    else:
        case["gen_metadata"] = {"mapping_source": "heuristic"}
    result = calc_point_coverage(_srr(1), [], [case])
    assert result["coverage_status"] == "unknown"
    assert result["covered_points"] == 0
    assert result["heuristic_point_ids"] == ["happy_path.1"]


@pytest.mark.parametrize("annotation", [
    {"notes": "原模型标注 [happy_path.1]", "tags": ["原始标签"]},
    {"covered_point_ids": ["happy_path.1"], "tags": ["原始标签"]},
    {"gen_metadata": {"covered_points": ["happy_path.1"], "notes": "原始元数据"}},
])
def test_backfill_preserves_original_explicit_evidence(annotation):
    case = {"title": "校验业务步骤1", **deepcopy(annotation)}
    before = deepcopy(case)
    assert backfill_cases_point_ids([case], _srr(1), []) == 0
    assert case == before
    result = calc_point_coverage(_srr(1), [], [case])
    assert result["coverage_status"] == "mapped"
    assert result["coverage_rate"] == 1.0
    assert result["heuristic_point_ids"] == []


def test_mixed_explicit_and_heuristic_cases_keep_only_explicit_evidence():
    cases = [{"notes": "[happy_path.1]"}, {"title": "校验业务步骤2"}]
    backfill_cases_point_ids(cases, _srr(2), [])
    result = calc_point_coverage(_srr(2), [], cases)
    assert result["coverage_status"] == "mapped"
    assert result["coverage_rate"] == 0.5
    assert result["covered_point_ids"] == ["happy_path.1"]
    assert result["cases_with_valid_ids"] == 1
    assert result["uncovered"][0]["id"] == "happy_path.2"
    assert "happy_path.2" in result["heuristic_point_ids"]


def test_invalid_existing_labels_are_preserved_not_replaced_by_heuristics():
    case = {"title": "校验业务步骤1", "covered_point_ids": ["happy_path.99"]}
    before = deepcopy(case)
    assert backfill_cases_point_ids([case], _srr(1), []) == 0
    assert case == before
    result = calc_point_coverage(_srr(1), [], [case])
    assert result["coverage_status"] == "unknown"
    assert result["invalid_point_ids"] == ["happy_path.99"]


def test_precondition_annotation_is_legal_but_not_counted():
    """前置条件是合法要点类型：不得判为"引用不存在"，也不进覆盖分母。

    端到端实测发现的假阻断：用例标注 precondition.1 → invalid_point_references
    → 发布决策被阻断（而它其实是合法标注）。
    """
    srr = _srr(1)
    srr["preconditions"] = ["用户已登录"]
    case = {"notes": "[precondition.1] [happy_path.1]"}
    result = calc_point_coverage(srr, [], [case])
    assert result["invalid_point_ids"] == []
    assert result["total_points"] == 1  # 分母仍不含前置条件
    assert result["covered_points"] == 1
    assert result["covered_point_ids"] == ["happy_path.1"]
    assert result["coverage_status"] == "mapped"


def test_unknown_precondition_still_reported_invalid():
    srr = _srr(1)
    srr["preconditions"] = ["用户已登录"]
    result = calc_point_coverage(srr, [], [{"notes": "[precondition.9]"}])
    assert result["invalid_point_ids"] == ["precondition.9"]
    assert result["covered_points"] == 0


def test_normalize_structured_ids_drops_unknown_keeps_valid():
    """prompt 改为要求结构化 covered_point_ids 后，编造的编号必须被剔除——
    否则 invalid_point_references 会阻断发布。"""
    from app.utils.coverage import normalize_structured_point_ids
    srr = _srr(2)
    srr["preconditions"] = ["用户已登录"]
    cases = [
        {"covered_point_ids": ["happy_path.1", "hallucinated.9"]},   # 混入编造编号
        {"covered_point_ids": ["precondition.1", "happy_path.2"]},   # 前置条件合法
        {"covered_point_ids": []},                                    # 空：不动
        {"title": "无结构化字段"},                                    # 缺字段：不动
    ]
    fixed = normalize_structured_point_ids(cases, srr, [])
    assert fixed == 1
    assert cases[0]["covered_point_ids"] == ["happy_path.1"]          # 编造的被剔
    assert cases[1]["covered_point_ids"] == ["precondition.1", "happy_path.2"]  # 合法保留
    assert "covered_point_ids" not in cases[3]


def test_normalize_does_not_touch_notes_annotation():
    """notes 覆盖段合法标注不动——只修复清单外编号。"""
    from app.utils.coverage import normalize_structured_point_ids
    cases = [{"notes": "覆盖: [happy_path.1]", "covered_point_ids": ["happy_path.1"]}]
    assert normalize_structured_point_ids(cases, _srr(1), []) == 0
    assert cases[0]["notes"] == "覆盖: [happy_path.1]"


def test_normalize_fixes_bare_id_in_notes_via_related_fp():
    """自举验证实测缺陷（防线不对称）：FP 分片生成时 LLM 在 notes 写裸形态
    [rule.1]（清单实际是 REQ-xxx.rule.1），旧版 normalize 只清结构化字段不清
    notes → 裸 ID 被 extract 捞出 → invalid_point_references 全批阻断。
    修复后：notes 覆盖段的裸 ID 按 related_fp 补前缀挽救。"""
    from app.utils.coverage import normalize_structured_point_ids
    fp_id = "REQ-001"
    fp = {
        "id": fp_id,
        "name": "登录",
        "srr_json": {
            "requirement": {"id": fp_id, "priority": "P1"},
            "flows": {"happy_path": {"steps": [{"action": "输入密码"}]}},
            "business_rules": [{"rule": "密码 8 位起"}],
        },
    }
    srr = _srr(1)
    cases = [{
        "related_fp": fp_id,
        "covered_point_ids": ["happy_path.1", f"{fp_id}.happy_path.1", f"{fp_id}.rule.1"],
        "notes": "覆盖: [happy_path.1] [rule.1]；技术: 场景法",
        "gen_metadata": {"notes": "覆盖: [happy_path.1] [rule.1]"},
    }]
    fixed = normalize_structured_point_ids(cases, srr, [fp])
    assert fixed == 1
    # notes 覆盖段：裸 rule.1 → 前缀补全（结构化字段本已合法，不动）
    assert cases[0]["notes"] == "覆盖: [happy_path.1] [" + fp_id + ".rule.1]；技术: 场景法"
    assert cases[0]["gen_metadata"]["notes"] == "覆盖: [happy_path.1] [" + fp_id + ".rule.1]"
    # 修复后 extract 不再产生裸 ID
    assert extract_point_ids(cases[0]) == {"happy_path.1", f"{fp_id}.happy_path.1", f"{fp_id}.rule.1"}


def test_normalize_drops_unmappable_bare_id_in_notes():
    """related_fp 为空或映射不上的裸 ID：从 notes 覆盖段剔除，正文不动。"""
    from app.utils.coverage import normalize_structured_point_ids
    cases = [{
        "covered_point_ids": ["happy_path.1"],
        "notes": "覆盖: [happy_path.1] [rule.99]；技术: 边界值",
    }]
    fixed = normalize_structured_point_ids(cases, _srr(1), [])
    assert fixed == 1
    assert cases[0]["notes"] == "覆盖: [happy_path.1]；技术: 边界值"
    assert cases[0]["covered_point_ids"] == ["happy_path.1"]


def test_normalize_prefixed_bare_id_in_structured_field_rescued():
    """结构化字段里的裸 ID：related_fp 能映射 → 补前缀挽救（旧版直接剔）。"""
    from app.utils.coverage import normalize_structured_point_ids
    fp_id = "REQ-002"
    fp = {
        "id": fp_id,
        "name": "上传",
        "srr_json": {
            "requirement": {"id": fp_id, "priority": "P1"},
            "business_rules": [{"rule": "单文件 ≤10MB"}],
        },
    }
    srr = _srr(1)
    cases = [{"related_fp": fp_id, "covered_point_ids": ["happy_path.1", "rule.1"]}]
    fixed = normalize_structured_point_ids(cases, srr, [fp])
    assert fixed == 1
    assert cases[0]["covered_point_ids"] == ["happy_path.1", f"{fp_id}.rule.1"]



# ============================================================================
# v3.5.11：分组维度覆盖探针（docs/04 类型体系定稿）
# 主口径不变（要点被任一用例覆盖即计 covered），增量呈现"覆盖手段结构"。
# ============================================================================

def test_group_probes_record_coverage_structure():
    """功能组主流程 + 边界手段 → 分组探针记录手段结构，主口径不变。"""
    srr = _srr(2)
    cases = [
        # happy_path.1：functional 主流程覆盖
        {"type": "functional", "covered_point_ids": ["happy_path.1"]},
        # happy_path.2：仅 boundary 手段覆盖（无 functional 组用例）→ 探针应点名
        {"type": "boundary", "covered_point_ids": ["happy_path.2"]},
    ]
    r = calc_point_coverage(srr, [], cases)
    # 主口径不受影响
    assert r["covered_points"] == 2
    assert r["group_case_distribution"] == {"functional": 2}  # boundary 归功能组
    assert r["covered_point_groups"]["happy_path.1"] == ["functional"]
    assert r["covered_point_groups"]["happy_path.2"] == ["functional"]
    # boundary 也归 functional 组 → 不会被点名（设计使然：组内手段不区分）
    assert r["points_without_functional_group"] == []


def test_group_probes_flag_non_functional_only_coverage():
    """仅 security/performance 手段覆盖的要点 → points_without_functional_group 点名。"""
    srr = _srr(2)
    cases = [
        {"type": "security", "covered_point_ids": ["happy_path.1"]},
        {"type": "performance", "covered_point_ids": ["happy_path.2"]},
    ]
    r = calc_point_coverage(srr, [], cases)
    assert r["group_case_distribution"] == {"security": 1, "performance": 1}
    assert r["points_without_functional_group"] == ["happy_path.1", "happy_path.2"]


def test_group_probes_ignore_heuristic_mapping():
    """heuristic 回填用例不进分组统计（与主口径一致：不计覆盖分子）。"""
    srr = _srr(1)
    cases = [{"type": "boundary", "covered_point_ids": ["happy_path.1"],
              "mapping_source": "heuristic",
              "gen_metadata": {"notes": "覆盖: [happy_path.1]"}}]
    r = calc_point_coverage(srr, [], cases)
    # 主口径与本探针一致：heuristic 只登记、不占覆盖分子
    assert r["covered_points"] == 0
    assert r["heuristic_point_ids"] == ["happy_path.1"]
    assert r["group_case_distribution"] == {}
    assert r["covered_point_groups"] == {}
    assert r["points_without_functional_group"] == []
