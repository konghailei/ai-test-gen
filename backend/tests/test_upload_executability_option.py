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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
