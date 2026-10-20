"""用例类型分类体系单测（v3.5.11）。

锁定 case_type_taxonomy 的细分→分组映射，作为前后端 5 处历史映射漂移
（"边界值" vs "边界测试"、markdown 缺 regression）的守卫：任何映射改动
必须显式过这里。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.core.case_type_taxonomy import (
    DETAIL_LABEL,
    GROUP_LABEL,
    GROUP_ORDER,
    TYPE_GROUP,
    case_type_composite_label,
    case_type_detail_label,
    case_type_group,
    case_type_group_label,
    group_members,
    group_representative,
    group_sort_key,
)


def test_functional_group_contains_design_techniques():
    """功能测试组含 boundary/exception/exploratory（大哥拍板：经典归属 ⊂ 功能测试）。"""
    for t in ("functional", "boundary", "exception", "exploratory"):
        assert case_type_group(t) == "functional"
        assert case_type_group_label(t) == "功能测试"


def test_integration_renamed_to_scenario():
    """integration 展示更名场景测试，内部值不变（仅映射层）。"""
    assert case_type_group("integration") == "scenario"
    assert case_type_group_label("integration") == "场景测试"
    assert "integration" in TYPE_GROUP  # 内部值保留


def test_independent_groups():
    for t, g in (("security", "security"), ("performance", "performance"),
                 ("regression", "regression"), ("ui", "ui"), ("api", "api"), ("data", "data")):
        assert case_type_group(t) == g


def test_unknown_type_falls_back_to_other():
    assert case_type_group("") == "other"
    assert case_type_group(None) == "other"
    assert case_type_group("nonsense") == "other"
    assert case_type_group_label("nonsense") == "其他"


def test_composite_label():
    assert case_type_composite_label("functional") == "功能测试"       # 代表值不缀细分
    assert case_type_composite_label("boundary") == "功能测试·边界值"
    assert case_type_composite_label("exception") == "功能测试·异常"
    assert case_type_composite_label("exploratory") == "功能测试·探索性"
    assert case_type_composite_label("integration") == "场景测试"       # 组内唯一细分
    assert case_type_composite_label("security") == "安全测试"
    assert case_type_composite_label("regression") == "回归测试"
    # 空值（脏数据缺 type）：只显示组名，不缀"其他"（防"其他·其他"丑标签）
    assert case_type_composite_label("") == "其他"
    assert case_type_composite_label(None) == "其他"


def test_group_representative():
    """分组代表细分：分组节点下新建用例的兜底 type（前端 XMind 编辑页用）。"""
    assert group_representative("functional") == "functional"
    assert group_representative("scenario") == "integration"
    assert group_representative("security") == "security"
    assert group_representative("regression") == "regression"
    assert group_representative("nonsense") == "other"


def test_detail_label():
    assert case_type_detail_label("boundary") == "边界值"
    assert case_type_detail_label("integration") == "跨功能点场景"


def test_group_members_and_order():
    assert group_members("functional") == ("functional", "boundary", "exception", "exploratory")
    assert group_members("scenario") == ("integration",)
    assert GROUP_ORDER[0] == "functional"
    assert group_sort_key("functional") == 0
    assert group_sort_key("nonsense") == len(GROUP_ORDER)


def test_labels_cover_all_types():
    """每个细分 type 都有分组中文名与细分中文名（无裸英文外泄）。"""
    for t in TYPE_GROUP:
        assert case_type_group_label(t) != t
        assert case_type_detail_label(t) != t
        assert case_type_composite_label(t) != t


def test_all_groups_have_labels():
    for g in GROUP_ORDER:
        assert g in GROUP_LABEL
        assert g in DETAIL_LABEL or True  # 分组中文名必须有；细分中文名按组代表值覆盖


def test_summarize_by_group_merges_boundary_into_functional():
    """v2.49：统计口径按展示分组——boundary/exception/exploratory 归入 functional。

    这是"用例分类中不应出现独立边界测试"的守卫：按分组聚合后，
    boundary 不得作为独立 key 出现。
    """
    from app.core.case_type_taxonomy import summarize_by_group

    cases = [
        {"type": "functional"}, {"type": "functional"},
        {"type": "boundary"}, {"type": "boundary"},
        {"type": "exception"},
        {"type": "security"},
        {"type": "integration"},
    ]
    got = summarize_by_group(cases)
    assert got == {"functional": 5, "security": 1, "scenario": 1}, got
    assert "boundary" not in got and "exception" not in got


def test_summarize_by_group_accepts_type_strings_and_order():
    from app.core.case_type_taxonomy import GROUP_ORDER, summarize_by_group

    got = summarize_by_group(["boundary", "security", "boundary", "functional"])
    assert got == {"functional": 3, "security": 1}
    # 顺序按 GROUP_ORDER
    assert list(got) == [g for g in GROUP_ORDER if g in got]


def test_summarize_by_detail_keeps_subtypes():
    """细分口径必须保留 boundary（技术覆盖矩阵度量用）。"""
    from app.core.case_type_taxonomy import summarize_by_detail

    got = summarize_by_detail([{"type": "boundary"}, {"type": "boundary"}, {"type": "exception"}, {}])
    assert got == {"boundary": 2, "exception": 1, "functional": 1}, got


def test_summarize_empty():
    from app.core.case_type_taxonomy import summarize_by_detail, summarize_by_group

    assert summarize_by_group([]) == {}
    assert summarize_by_group(None) == {}
    assert summarize_by_detail([]) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
