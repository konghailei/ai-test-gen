# -*- coding: utf-8 -*-
"""可执行性分级单测（v2.49）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.case_executability import grade_case, grade_cases  # noqa: E402
from app.core.case_path_lint import allowed_path_patterns  # noqa: E402

SRR = {
    "api_definitions": [{"path": "/api/health"}],
    "_raw_text": "探活接口 GET /api/health，返回 version。",
}
PATTERNS = allowed_path_patterns(SRR)


def _case(**kw) -> dict:
    base = {
        "id": "TC-1", "title": "查询健康状态", "expected": "返回 4.1.1",
        "steps": [{"action": "调用 GET /api/health", "expected": "返回 version"}],
        "assertions": [{"type": "equals", "target": "response.body.data.version",
                        "expected_value": "4.1.1"}],
        "test_data": {"base_url": "http://127.0.0.1:8000"},
        "notes": "覆盖: [api.1]",
    }
    base.update(kw)
    return base


def test_grade_a_complete_case():
    r = grade_case(_case(), PATTERNS, SRR["_raw_text"])
    assert r["grade"] == "A", r
    assert not r["fabricated"]


def test_grade_b_abstract_assertion():
    r = grade_case(_case(assertions=[{"type": "equals", "target": "response.code"}]), PATTERNS, SRR["_raw_text"])
    assert r["grade"] == "B"
    assert any("抽象命名空间" in x for x in r["reasons"])


def test_grade_b_unlocated_ui_step():
    r = grade_case(_case(steps=[{"action": "查看上传结果", "expected": "显示成功"}]),
                   PATTERNS, SRR["_raw_text"])
    assert r["grade"] == "B"
    assert any("无定位" in x for x in r["reasons"])


def test_grade_b_missing_test_data():
    r = grade_case(_case(test_data={}), PATTERNS, SRR["_raw_text"])
    assert r["grade"] == "B"
    assert any("test_data" in x for x in r["reasons"])


def test_grade_c_gap_marker_is_honest_not_fabricated():
    r = grade_case(_case(notes="缺口: 入口定位；字段名"), PATTERNS, SRR["_raw_text"])
    assert r["grade"] == "C"
    assert not r["fabricated"], "缺口标注是诚实降级，不算编造"


def test_grade_c_fabricated_path():
    r = grade_case(
        _case(steps=[{"action": "调用 /api/llm/fallback 触发熔断", "expected": "禁用 600s"}]),
        PATTERNS, SRR["_raw_text"],
    )
    assert r["grade"] == "C"
    assert r["fabricated"] is True
    assert any("无需求依据" in x for x in r["reasons"])


def test_batch_stats_and_fabricated_rate():
    cases = [
        _case(id="TC-A"),
        _case(id="TC-B", assertions=[{"type": "equals", "target": "page.text"}]),
        _case(id="TC-C", steps=[{"action": "调用 /api/v1/x", "expected": ""}]),
        _case(id="TC-D", notes="缺口: 入口待补充"),
    ]
    st = grade_cases(cases, SRR)
    assert st["checked"] == 4
    assert st["A"] == 1 and st["B"] == 1 and st["C"] == 2
    assert st["fabricated"] == 1
    assert st["fabricated_rate"] == 0.25
    # 就地写入审计字段
    assert cases[0]["_executability"]["grade"] == "A"
    assert cases[2]["_executability"]["grade"] == "C"


def test_fail_open_without_contract():
    """无可信路径集合时不能判编造（fail-open），其余维度照常。"""
    st = grade_cases([_case(steps=[{"action": "调用 /api/whatever", "expected": ""}])],
                     {"api_definitions": [], "_raw_text": ""})
    assert st["skipped"] is True
    assert st["fabricated"] == 0


def test_malformed_input():
    assert grade_cases(None, SRR)["checked"] == 0
    assert grade_cases([None, "x"], SRR)["checked"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
