# -*- coding: utf-8 -*-
"""可执行性要素缺口检测单测（v2.49）。

对应 R4 实测的四类主因：入口定位 / 字段响应 / 场景数据 / 故障注入。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agents.clarify_question_gen import ClarifyQuestionGenMixin  # noqa: E402


def _run(raw: str, srr_extra: dict | None = None) -> list[dict]:
    srr = {"_raw_text": raw}
    if srr_extra:
        srr.update(srr_extra)
    out: list[dict] = []
    ClarifyQuestionGenMixin._detect_executability_gaps(raw, srr, out)
    return out


def _fields(out: list[dict]) -> set[str]:
    return {d.get("field", "") for d in out}


def test_ui_entry_gap_detected():
    out = _run("用户可以在需求管理页面查看列表，并按状态筛选。")
    assert "prd.executability.ui_entry" in _fields(out)


def test_ui_entry_not_flagged_when_entry_present():
    out = _run("用户从左侧导航菜单进入需求管理页面，查看列表并筛选。")
    assert "prd.executability.ui_entry" not in _fields(out)


def test_api_response_gap_detected():
    out = _run(
        "接口 /api/requirements 用于查询需求。",
        {"api_definitions": [{"path": "/api/requirements", "response_body": ""}]},
    )
    assert "prd.executability.api_response" in _fields(out)


def test_api_response_not_flagged_when_specified():
    out = _run(
        "接口 /api/requirements 返回 total/items。",
        {"api_definitions": [{"path": "/api/requirements", "response_body": "total/items"}]},
    )
    assert "prd.executability.api_response" not in _fields(out)


def test_scene_data_gap_detected():
    out = _run("支持多用户并发编辑，使用乐观锁防止冲突。")
    assert "prd.executability.scene_data" in _fields(out)


def test_scene_data_not_flagged_when_baseline_given():
    out = _run("支持多用户并发编辑；测试需 2 个账号（管理员 + 普通用户）。")
    assert "prd.executability.scene_data" not in _fields(out)


def test_fault_inject_gap_detected():
    out = _run("当向量库不可用时，系统降级为 PG 检索。")
    assert "prd.executability.fault_inject" in _fields(out)


def test_fault_inject_not_flagged_when_switch_given():
    out = _run("向量库不可用时降级为 PG 检索；通过配置开关模拟不可用。")
    assert "prd.executability.fault_inject" not in _fields(out)


def test_no_duplicate_when_called_twice():
    out: list[dict] = []
    raw = "用户可以在需求管理页面查看列表。"
    ClarifyQuestionGenMixin._detect_executability_gaps(raw, {"_raw_text": raw}, out)
    ClarifyQuestionGenMixin._detect_executability_gaps(raw, {"_raw_text": raw}, out)
    assert len([d for d in out if d["field"] == "prd.executability.ui_entry"]) == 1


def test_each_gap_has_question_and_options():
    out = _run("用户可以在需求管理页面查看列表。")
    for d in out:
        assert d["category"] == "gap"
        assert d["suggested_question"].strip()
        assert len(d["suggested_options"]) >= 2


def test_switch_off_disables_detection(monkeypatch=None):
    """CLARIFY_EXECUTABILITY_GAPS_ENABLED=False 时 _detect_prd_specific_gaps 不追加可执行性 gap。"""
    from app.agents.clarify_question_gen import settings

    old = settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED
    try:
        settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED = False
        gaps, _ = ClarifyQuestionGenMixin._detect_prd_specific_gaps(
            None, {"_raw_text": "用户可以在需求管理页面查看列表。"}
        )
        assert not any(d.get("field", "").startswith("prd.executability.") for d in gaps)
    finally:
        settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED = old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
