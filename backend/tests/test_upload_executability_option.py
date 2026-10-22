# -*- coding: utf-8 -*-
"""需求级可执行性追问开关单测（v2.49）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agents.clarify_question_gen import ClarifyQuestionGenMixin  # noqa: E402
from app.models.api_schemas import RequirementUploadRequest  # noqa: E402

class _Gen(ClarifyQuestionGenMixin):
    """测试用最小实例（mixin 无 __init__ 依赖）。"""
    pass


RAW = "用户可以在需求管理页面查看列表，并按状态筛选。"


def test_upload_request_default_enabled():
    r = RequirementUploadRequest(raw_text="x")
    assert r.executability_clarify is True


def test_upload_request_can_disable():
    r = RequirementUploadRequest(raw_text="x", executability_clarify=False)
    assert r.executability_clarify is False


def test_requirement_level_off_suppresses_exec_gaps():
    """需求级关闭 → 即使系统总闸开着，也不产出可执行性 gap。"""
    srr = {"_raw_text": RAW, "_clarify_options": {"executability_check": False}}
    gaps, _ = _Gen()._detect_prd_specific_gaps(srr)
    assert not any(d.get("field", "").startswith("prd.executability.") for d in gaps)


def test_requirement_level_on_produces_gaps():
    srr = {"_raw_text": RAW, "_clarify_options": {"executability_check": True}}
    gaps, _ = _Gen()._detect_prd_specific_gaps(srr)
    assert any(d.get("field", "").startswith("prd.executability.") for d in gaps)


def test_missing_option_defaults_to_enabled():
    """旧数据/未注入选项时按默认开启（保持现状行为）。"""
    gaps, _ = _Gen()._detect_prd_specific_gaps({"_raw_text": RAW})
    assert any(d.get("field", "").startswith("prd.executability.") for d in gaps)


# ============================================================================
# v4.1.2：可执行性缺口「确定性落地」——规则层检出必须到达 questions
# ============================================================================
# 为什么断言放在这一层：v2.49 实测的缺陷不在检测，而在交付。规则层稳定检出
# 8 条 gap，但只进 `important_missing` 当 LLM 参考，最终 0 条成为问题。
# 因此这些用例**必须强断言**（问题列表里出现/不出现），不能软断言。
# 断言按结构字段（field / _source / input_type）判定，不匹配问题文案措辞。

from app.agents.clarify_agent import ClarifyAgent  # noqa: E402
from app.agents.clarify_question_gen import settings as _cq_settings  # noqa: E402


def _exec_qs(srr: dict) -> list[dict]:
    return ClarifyAgent()._executability_gap_questions(srr)


def _exec_fields(qs: list[dict]) -> set[str]:
    return {q.get("field", "") for q in qs}


def test_exec_gap_reaches_questions_deterministically():
    """规则层检出的 4 类缺口全部确定性成为澄清问题（一次调用即成立，无 LLM 参与）。"""
    raw = (
        "用户可以在需求管理页面查看列表并提交表单。"
        "支持多用户并发编辑，使用乐观锁防止冲突。"
        "当向量库不可用时，系统降级为 PG 检索。"
    )
    srr = {
        "_raw_text": raw,
        "api_definitions": [{"path": "/api/requirements", "response_body": ""}],
    }
    qs = _exec_qs(srr)
    fields = _exec_fields(qs)
    assert "prd.executability.ui_entry" in fields
    assert "prd.executability.api_response" in fields
    assert "prd.executability.scene_data" in fields
    assert "prd.executability.fault_inject" in fields


def test_exec_gap_question_structure():
    """注入项字段结构符合澄清问题契约（下游 dedup / 前端渲染依赖）。"""
    qs = _exec_qs({"_raw_text": "用户可以在需求管理页面查看列表。"})
    assert qs, "应至少注入 1 条可执行性缺口问题"
    for q in qs:
        assert q["id"].startswith("EXG")
        assert q["_source"] == "executability_gap"
        assert q["priority"] == "important"
        # 下游语义检测层只接受 completeness/ambiguity/conflict
        assert q["category"] in ("completeness", "ambiguity", "conflict")
        assert q["question"].strip()
        assert q["field"].startswith("prd.executability.")
        if q["options"]:
            assert q["input_type"] == "select"
            assert len(q["options"]) >= 2


def test_exec_gap_survives_necessity_filter():
    """注入项必须能通过必要性过滤（否则等于白注入）。"""
    from app.agents.clarify_question_gen import ClarifyQuestionGenMixin

    qs = _exec_qs({"_raw_text": "用户可以在需求管理页面查看列表。"})
    kept = ClarifyQuestionGenMixin._filter_questions_by_necessity(qs)
    assert len(kept) == len(qs)


def test_exec_gap_survives_options_validation():
    """注入项必须能通过 options 校验（防止被判错乱而降级丢选项）。"""
    from app.agents.clarify_question_gen import ClarifyQuestionGenMixin

    qs = _exec_qs({"_raw_text": "用户可以在需求管理页面查看列表。"})
    fixed = ClarifyQuestionGenMixin._validate_and_fix_options(qs)
    assert len(fixed) == len(qs)
    assert fixed[0]["options"], "入口定位问题的选项不应被清空"


def test_exec_gap_no_duplicate_when_already_present():
    """重复调用不产生重复问题（幂等）。"""
    srr = {"_raw_text": "用户可以在需求管理页面查看列表。"}
    a = _exec_qs(srr)
    b = _exec_qs(srr)
    assert _exec_fields(a) == _exec_fields(b)


def test_exec_gap_system_switch_off_blocks_injection():
    """系统总闸关闭 → 不注入（关掉就真的不问）。"""
    old = _cq_settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED
    try:
        _cq_settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED = False
        assert _exec_qs({"_raw_text": "用户可以在需求管理页面查看列表。"}) == []
    finally:
        _cq_settings.CLARIFY_EXECUTABILITY_GAPS_ENABLED = old


def test_exec_gap_requirement_switch_off_blocks_injection():
    """需求级关闭 → 不注入（即使系统总闸开着）。"""
    srr = {
        "_raw_text": "用户可以在需求管理页面查看列表。",
        "_clarify_options": {"executability_check": False},
    }
    assert _exec_qs(srr) == []


def test_exec_gap_empty_raw_text_returns_empty():
    """无原文（如 KB 补充路径）→ 静默不注入，且不得抛异常。"""
    assert _exec_qs({}) == []
    assert _exec_qs({"_raw_text": ""}) == []


def test_exec_gap_max_cap():
    """单次注入不超过 _EXEC_GAP_MAX（防止问题列表被规则项刷屏）。"""
    qs = _exec_qs({
        "_raw_text": (
            "用户可以在需求管理页面查看列表并提交表单。"
            "支持多用户并发编辑，使用乐观锁防止冲突。"
            "当向量库不可用时，系统降级为 PG 检索。"
        ),
        "api_definitions": [{"path": "/api/requirements", "response_body": ""}],
    })
    assert len(qs) <= ClarifyAgent._EXEC_GAP_MAX


def test_executability_fields_excluded_from_kb_fallback_queries():
    """可执行性缺口不得进入 KB 兜底查询路径（v4.1.2 根因守卫）。

    背景：`_fallback_gap_queries` 会读 completeness["important_missing"]，
    而 ②b 会把 prd.executability.* 塞进去。若这里放行，就会：
      ① 必然 answered=0，挤占 MAX_KB_QUERIES 名额；
      ② 经模板重建后 `_source`/`input_type`/`options` 全部丢失（4 个候选入口被清空）；
      ③ 与 ⑧f 确定性注入重复，语义去重后可能留下退化版。
    本条断言的是**行为**（不产出），不是字面量。
    """
    from app.agents.clarify_detect import ClarifyDetectMixin

    completeness = {
        "critical_missing": [],
        "important_missing": [
            {
                "field": "prd.executability.ui_entry",
                "description": "未说明入口",
                "_suggested_question": "该功能位于哪个菜单？",
                "_suggested_options": ["顶部导航", "左侧菜单"],
            },
            {
                "field": "prd.executability.fault_inject",
                "description": "未说明故障注入",
                "_suggested_question": "故障如何模拟？",
                "_suggested_options": ["配置开关", "断开服务"],
            },
            # 对照项：普通缺口**必须**照常进入 KB 查询，否则本次修复变成了误杀
            {
                "field": "flows.business_rules",
                "description": "缺少业务规则",
                "_suggested_question": "业务规则是什么？",
            },
        ],
        "optimization_missing": [],
    }
    qs = ClarifyDetectMixin._fallback_gap_queries(
        ClarifyAgent(),
        completeness,
        False,   # is_incremental
        set(),   # covered_fields：空集，让所有缺口都有机会进入
        None,    # srr
    )
    fields = {q.get("field", "") for q in qs}
    assert not any(f.startswith("prd.executability.") for f in fields), (
        f"可执行性缺口不应进入 KB 查询，实际: {fields}"
    )
    assert "flows.business_rules" in fields, "普通缺口应照常进入 KB 查询（不要误杀）"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
