# -*- coding: utf-8 -*-
"""B-3（docs/32 / F3）：安全负向断言方向写反的确定性检测 + pytest 导出负向算子。

验收（docs/32 B-3）：
1. 检测双条件缺一不判（message 负向词 ∧ 载荷模式），正反各 3 例；
2. 修复后断言（not_contains/not_in）不被误伤；
3. pytest 导出器把 not_contains/not_equals/not_in 转为可执行负向 assert
   （修复闭环：否则翻转后断言只是注释，执行面零变化）。

type 口径（⚠ 定稿偏差，实证优先）：docs/32 定稿 type ∈ {contains, equals}，
DB 全量定标 32 条全为 custom → 检测器纳入 custom、豁免 not_*、排除
greater_or_equal/less_or_equal。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.exporters.pytest_code import _assertion_to_code  # noqa: E402
from app.utils.case_validation import (  # noqa: E402
    GeneratedCaseValidationError,
    reversed_security_assertion_issues,
    validate_generated_cases,
)


# ---------- 反例（应命中：双条件齐备） ----------

def test_hit_custom_payload():
    """实证主形态：custom + 负向词 + SQL 注入载荷。"""
    a = {
        "type": "custom", "target": "response.sanitized_answer",
        "message": "系统已过滤恶意SQL语句", "expected_value": "DROP TABLE",
    }
    assert reversed_security_assertion_issues(a, "cases[0].assertions[1]")


def test_hit_contains_xss_payload():
    """docs/31 F3 原始形态：contains 正向断言载荷被过滤 → 导出即假阴性。"""
    a = {
        "type": "contains", "target": "response.text",
        "message": "恶意脚本已被移除", "expected_value": "<script>alert(1)</script>",
    }
    assert reversed_security_assertion_issues(a, "x")


def test_hit_equals_js_payload():
    """equals 正向断言载荷本体 → 语义方向写反。"""
    a = {
        "type": "equals", "target": "sanitized",
        "message": "输入中的 javascript: 协议被拒绝", "expected_value": "javascript:alert(1)",
    }
    assert reversed_security_assertion_issues(a, "x")


# ---------- 正例（不应命中：缺一条件或方向正确） ----------

def test_miss_fixed_negative_operator():
    """修复后形态：not_contains + 负向词 + 载荷 = 合法「不得出现」对照。"""
    a = {
        "type": "not_contains", "target": "response.text",
        "message": "恶意脚本已被移除", "expected_value": "<script>alert(1)</script>",
    }
    assert not reversed_security_assertion_issues(a, "x")
    a2 = {
        "type": "not_in", "target": "response.text",
        "message": "SQL 注入载荷被拦截", "expected_value": "DROP TABLE",
    }
    assert not reversed_security_assertion_issues(a2, "x")


def test_miss_positive_assertion_no_payload():
    """合法正向断言：正向算子但无载荷模式（message 也无负向词）。"""
    a = {
        "type": "contains", "target": "response.headers",
        "message": "响应包含 Content-Security-Policy 头",
        "expected_value": "Content-Security-Policy",
    }
    assert not reversed_security_assertion_issues(a, "x")


def test_miss_negative_word_but_no_payload():
    """负向词有了，但 expected_value 不是攻击载荷 → 不判（防误伤）。"""
    a = {
        "type": "custom", "target": "response",
        "message": "非法字符已被过滤", "expected_value": "正常回显文本",
    }
    assert not reversed_security_assertion_issues(a, "x")


# ---------- 边界：非方向性算子豁免 ----------

def test_miss_greater_or_equal():
    """greater_or_equal 归一为 custom 但语义是数值比较，排除。"""
    a = {
        "type": "greater_or_equal", "target": "resp.count",
        "message": "拦截 <script> 的次数不少于 1", "expected_value": "<script>",
    }
    assert not reversed_security_assertion_issues(a, "x")


# ---------- 残余形态（定标补表：XSS 事件处理器族等） ----------

def test_hit_xss_event_handler():
    """残余主形态：onclick/onload/onmouseover 属性已过滤。"""
    a = {
        "type": "custom", "target": "sanitized_html",
        "message": "onclick属性已过滤", "expected_value": "onclick",
    }
    assert reversed_security_assertion_issues(a, "x")
    a2 = {
        "type": "custom", "target": "sanitized_html",
        "message": "script节点已移除", "expected_value": "script",
    }
    assert reversed_security_assertion_issues(a2, "x")


def test_hit_sql_boolean_payload():
    """SQL 布尔注入载荷 OR 1=1。"""
    a = {
        "type": "custom", "target": "response",
        "message": "注入语句被拦截", "expected_value": "OR 1=1",
    }
    assert reversed_security_assertion_issues(a, "x")


def test_miss_description_word_boundary():
    """裸 script 词边界匹配：description/manuscript 等 innocent 词不误伤。"""
    a = {
        "type": "custom", "target": "resp",
        "message": "非法字符已被过滤",
        "expected_value": "description",
    }
    assert not reversed_security_assertion_issues(a, "x")
    a2 = {
        "type": "contains", "target": "resp.text",
        "message": "文档 manuscript 被移除", "expected_value": "manuscript",
    }
    assert not reversed_security_assertion_issues(a2, "x")


def test_miss_status_code_rejection():
    """status_code + 负向词 + 数值 expected：方向正确（断言拒绝返回 4xx），不翻。"""
    a = {
        "type": "status_code", "target": "resp.status",
        "message": "非法请求被拒绝", "expected_value": 403,
    }
    assert not reversed_security_assertion_issues(a, "x")


def test_miss_reject_message_equals():
    """equals expected=拒绝提示文本：正向断言响应==提示语，方向正确，不翻。"""
    a = {
        "type": "equals", "target": "resp.message",
        "message": "文件被拒绝", "expected_value": "文件包含恶意代码，拒绝上传",
    }
    assert not reversed_security_assertion_issues(a, "x")


# ---------- 校验链路：走 invalid_case_content ----------

def test_validation_integration():
    """检测结果必须进 validation_issues（走 invalid_case_content → partial_block）。"""
    bad = {
        "requirement_id": "R-1", "title": "t", "preconditions": "p",
        "steps": [{"action": "a", "expected": "e"}],
        "assertions": [
            {"type": "custom", "message": "SQL 注入载荷被过滤",
             "expected_value": "DROP TABLE"},
        ],
    }
    try:
        validate_generated_cases([bad], "R-1")
    except GeneratedCaseValidationError as e:
        assert any("方向写反" in i for i in e.issues)
    else:
        raise AssertionError("方向写反断言应触发校验失败")


# ---------- pytest 导出器：负向算子必须可执行 ----------

def test_export_not_contains():
    code = _assertion_to_code({
        "type": "not_contains", "target": "response.text",
        "expected_value": "<script>", "message": "脚本被过滤",
    })
    assert code.startswith("assert")
    assert "not in" in code
    assert "<script>" in code


def test_export_not_equals():
    code = _assertion_to_code({
        "type": "not_equals", "target": "resp.status", "expected_value": 500,
    })
    assert "!=" in code


def test_export_not_in():
    code = _assertion_to_code({
        "type": "not_in", "target": "resp.body", "expected_value": ["a", "b"],
    })
    assert "not in" in code


def test_export_contains_unchanged():
    """正向 contains 映射不回归。"""
    code = _assertion_to_code({
        "type": "contains", "target": "resp.text", "expected_value": "ok",
    })
    assert '"ok" in resp.text' in code


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
