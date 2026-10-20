# -*- coding: utf-8 -*-
"""用例路径 lint 单测（v2.49）。

覆盖：臆造路径标记 / 契约路径放行 / 参数值通配不误报 / 原文路径放行 /
无契约 fail-open / 畸形输入。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.case_path_lint import lint_case_paths  # noqa: E402

SRR = {
    "api_definitions": [
        {"path": "/api/health", "method": "GET"},
        {"path": "/api/requirements/{req_id}/versions", "method": "GET"},
    ],
    "_raw_text": "可观测：向量出箱状态（`GET /api/observability/vector-outbox`）。",
}


def _case(cid: str, action: str, expected: str = "") -> dict:
    return {"id": cid, "steps": [{"action": action, "expected": expected}], "expected": ""}


def test_flags_hallucinated_path():
    cases = [_case("TC-1", "调用 /api/llm/fallback 触发熔断")]
    st = lint_case_paths(cases, SRR)
    assert st["flagged"] == 1
    assert cases[0]["_path_check"]["unverified_paths"] == ["/api/llm/fallback"]
    assert st["unverified_paths"]["/api/llm/fallback"] == ["TC-1"]


def test_allows_contract_path():
    cases = [_case("TC-2", "调用 /api/health 获取系统状态")]
    st = lint_case_paths(cases, SRR)
    assert st["flagged"] == 0
    assert "_path_check" not in cases[0]


def test_allows_raw_text_path():
    """原文中出现过的路径视为可信（需求文档给了路径）。"""
    cases = [_case("TC-3", "GET /api/observability/vector-outbox 查看积压")]
    st = lint_case_paths(cases, SRR)
    assert st["flagged"] == 0


def test_param_value_wildcard_no_false_positive():
    """契约写 {req_id}、用例写具体值 → 同构，不误报。"""
    cases = [_case("TC-4", "GET /api/requirements/REQ-1792-6157/versions 查询版本")]
    st = lint_case_paths(cases, SRR)
    assert st["flagged"] == 0, f"不应误报，实际: {st['unverified_paths']}"


def test_no_trusted_set_fail_open():
    cases = [_case("TC-5", "调用 /api/whatever")]
    st = lint_case_paths(cases, {"api_definitions": []})
    assert st["skipped"] is True
    assert st["flagged"] == 0
    assert "_path_check" not in cases[0]


def test_case_without_paths_untouched():
    cases = [_case("TC-6", "提交登录表单并检查提示")]
    st = lint_case_paths(cases, SRR)
    assert st["checked"] == 1
    assert st["flagged"] == 0


def test_malformed_input():
    assert lint_case_paths(None, SRR)["checked"] == 0
    assert lint_case_paths([], SRR)["checked"] == 0
    cases = [None, "x", {"id": "TC-7", "steps": []}]
    st = lint_case_paths(cases, SRR)
    assert st["checked"] == 1  # 只统计 dict


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception:
                fails += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{'全部通过' if not fails else f'{fails} 项失败'}")
    sys.exit(1 if fails else 0)
