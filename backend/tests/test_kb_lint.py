# -*- coding: utf-8 -*-
"""docs/18 P2：kb_lint 纯函数部分守卫单测（离线，不依赖 DB/Milvus）。"""
import pytest

from app.core.kb_lint import (
    detect_crsr_drift,
    detect_rule_contradictions,
    _norm_text,
)


# ---------------------------------------------------------------------------
# L1 漂移检测
# ---------------------------------------------------------------------------

def test_drift_identical_returns_empty():
    a = {"summary": "s", "business_rules": [{"id": "1", "rule": "r"}]}
    assert detect_crsr_drift(a, {"business_rules": [{"id": "1", "rule": "r"}], "summary": "s"}) == []


def test_drift_detects_scalar_and_nested_changes():
    consolidated = {"summary": "旧", "business_rules": [{"id": "1", "rule": "1-200"}]}
    recomputed = {"summary": "新", "business_rules": [{"id": "1", "rule": "1-500"}]}
    drift = detect_crsr_drift(consolidated, recomputed)
    assert set(drift) == {"summary", "business_rules"}


def test_drift_detects_added_or_removed_keys():
    assert detect_crsr_drift({"a": 1}, {"a": 1, "b": 2}) == ["b"]
    assert detect_crsr_drift({"a": 1, "c": 3}, {"a": 1}) == ["c"]


def test_drift_handles_non_dict():
    assert detect_crsr_drift(None, {"a": 1}) == ["<non-dict>"]
    assert detect_crsr_drift("x", {"a": 1}) == ["<non-dict>"]


# ---------------------------------------------------------------------------
# L2 矛盾检测
# ---------------------------------------------------------------------------

def test_norm_text_strips_punct_and_ws():
    assert _norm_text("1-200 个字符") == _norm_text("1~200个字符")
    assert _norm_text("长度：1～200") == _norm_text("长度1-200")


def test_contradiction_same_rule_id_different_text():
    versions = [
        {"req_id": "REQ-1", "srr": {"business_rules": [{"id": "BR-1", "rule": "1-200"}]}},
        {"req_id": "REQ-2", "srr": {"business_rules": [{"id": "BR-1", "rule": "1-500"}]}},
    ]
    out = detect_rule_contradictions(versions)
    assert len(out) == 1
    assert out[0]["kind"] == "rule_id"
    assert out[0]["key"] == "BR-1"
    assert {v["req_id"] for v in out[0]["versions"]} == {"REQ-1", "REQ-2"}


def test_no_contradiction_same_rule_normalized():
    """措辞变体（标点/空白差异）不算矛盾——与 CRSR 合并的规范化同口径。"""
    versions = [
        {"req_id": "REQ-1", "srr": {"business_rules": [{"id": "BR-1", "rule": "1-200 个字符"}]}},
        {"req_id": "REQ-2", "srr": {"business_rules": [{"id": "BR-1", "rule": "1~200个字符"}]}},
    ]
    assert detect_rule_contradictions(versions) == []


def test_contradiction_same_constraint_key_different_limits():
    versions = [
        {"req_id": "REQ-1", "srr": {"data_constraints": [
            {"field": "title", "type": "string", "required": True, "constraints": "1-200"}]}},
        {"req_id": "REQ-2", "srr": {"data_constraints": [
            {"field": "title", "type": "string", "required": True, "constraints": "1-500"}]}},
    ]
    out = detect_rule_contradictions(versions)
    assert len(out) == 1
    assert out[0]["kind"] == "constraint"
    assert "title" in out[0]["key"]


def test_no_contradiction_different_rule_ids():
    """不同 id 的规则内容不同 = 正常迭代，不是矛盾。"""
    versions = [
        {"req_id": "REQ-1", "srr": {"business_rules": [{"id": "BR-1", "rule": "A"}]}},
        {"req_id": "REQ-2", "srr": {"business_rules": [{"id": "BR-2", "rule": "B"}]}},
    ]
    assert detect_rule_contradictions(versions) == []


def test_contradiction_ignores_malformed_items():
    versions = [
        {"req_id": "REQ-1", "srr": {"business_rules": ["not-a-dict", None]}},
        {"req_id": "REQ-2", "srr": None},
        {"req_id": "REQ-3", "srr": "not-dict"},
    ]
    assert detect_rule_contradictions(versions) == []


# ---------------------------------------------------------------------------
# 灰度开关守卫
# ---------------------------------------------------------------------------

def test_lint_respects_enabled_flag(monkeypatch):
    """KB_LINT_ENABLED=False 时 start_kb_lint 不启动任务。"""
    import asyncio
    from app.core import kb_lint

    class _FakeSettings:
        KB_LINT_ENABLED = False
        KB_LINT_INTERVAL = 86400

    import app.config as cfg
    monkeypatch.setattr(cfg, "settings", _FakeSettings(), raising=False)

    # 全新事件循环里验证（避免污染 pytest 主循环）
    async def _run():
        task = kb_lint.start_kb_lint()
        assert task is None  # 不启动
        assert kb_lint._loop_task is None

    asyncio.run(_run())
