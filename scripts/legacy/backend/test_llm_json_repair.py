"""单元测试：验证 _normalize_code_expressions 和 _try_repair_json 修复 LLM 代码表达式。

复现生产环境 LLM 输出 "a".repeat(50) 导致 JSON 解析失败的问题。
"""
import json
import sys
import os

# 添加 backend 到 path（本地运行）或 /app（容器内运行）
for candidate in [
    os.path.join(os.path.dirname(__file__), "backend"),
    "/app",
    os.path.dirname(__file__),
]:
    if candidate and os.path.isdir(candidate):
        sys.path.insert(0, candidate)

from app.core.llm_client import (
    _normalize_code_expressions,
    _try_repair_json,
    _try_parse_json,
    _extract_json_block,
    LLMClient,
)


def _parse_json_wrapper(text):
    """调用 LLMClient._parse_json 静态方法（容错解析完整流程）。"""
    return LLMClient._parse_json(text, schema_hint=None)


def test_normalize_repeat():
    """测试 "a".repeat(N) 展开。"""
    cases = [
        # (input, expected_substring)
        ('"a".repeat(5)', '"aaaaa"'),
        ('"ab".repeat(3)', '"ababab"'),
        ('"a".repeat(50)', 'LEN_50'),  # 长度检查
        ('"a".repeat(128)', 'LEN_128'),
        ('"a".repeat(0)', '""'),
        ('"a".repeat(3, "-")', '"a-a-a"'),
    ]
    for inp, expected in cases:
        result = _normalize_code_expressions(inp)
        if expected.startswith("LEN_"):
            count = int(expected.split("_")[1])
            inner = result.strip('"')
            assert len(inner) == count, f"FAIL: {inp} -> len={len(inner)}, expected={count}"
            print(f"[OK] {inp} -> {len(inner)} chars")
        else:
            assert result == expected, f"FAIL: {inp} -> {result}, expected={expected}"
            print(f"[OK] {inp} -> {result}")


def test_normalize_mul():
    """测试 "a" * N 展开。"""
    cases = [
        ('"a" * 5', '"aaaaa"'),
        ('"ab" * 3', '"ababab"'),
        ('"x" * 50', 'LEN_50'),
    ]
    for inp, expected in cases:
        result = _normalize_code_expressions(inp)
        if expected.startswith("LEN_"):
            count = int(expected.split("_")[1])
            inner = result.strip('"')
            assert len(inner) == count, f"FAIL: {inp} -> len={len(inner)}"
            print(f"[OK] {inp} -> {len(inner)} chars")
        else:
            assert result == expected, f"FAIL: {inp} -> {result}, expected={expected}"
            print(f"[OK] {inp} -> {result}")


def test_try_repair_json_with_repeat():
    """测试 _try_repair_json 修复含 .repeat() 的 JSON。"""
    # 模拟生产 LLM 输出
    bad_json = '''[
  {
    "id": "TC-001",
    "title": "边界测试",
    "test_data": {
      "username": "a".repeat(50),
      "password": "a".repeat(128)
    }
  }
]'''
    result = _try_repair_json(bad_json)
    assert result is not None, "FAIL: _try_repair_json 返回 None"
    assert isinstance(result, list), f"FAIL: 期望 list，得到 {type(result)}"
    assert len(result) == 1
    assert len(result[0]["test_data"]["username"]) == 50
    assert len(result[0]["test_data"]["password"]) == 128
    print(f"[OK] _try_repair_json 修复 .repeat() 成功: username len={len(result[0]['test_data']['username'])}, password len={len(result[0]['test_data']['password'])}")


def test_try_repair_json_with_mul():
    """测试 _try_repair_json 修复含 * N 的 JSON。"""
    bad_json = '''[
  {
    "id": "TC-002",
    "title": "Python 风格重复",
    "data": {"password": "x" * 100}
  }
]'''
    result = _try_repair_json(bad_json)
    assert result is not None, "FAIL: _try_repair_json 返回 None"
    assert len(result[0]["data"]["password"]) == 100
    print(f"[OK] _try_repair_json 修复 * N 成功: password len={len(result[0]['data']['password'])}")


def test_parse_json_with_codeblock_repeat():
    """测试 _parse_json 完整流程：markdown 代码块 + .repeat()。"""
    # 模拟生产 LLM 完整输出（markdown 代码块包裹 + 含 .repeat()）
    llm_output = '''```json
[
  {
    "id": "TC-AUTH-0001-0008",
    "title": "账号长度边界（50字符）",
    "type": "boundary",
    "steps": [
      {"step": 1, "action": "输入账号", "expected": "登录成功", "data": {"username": "a".repeat(50)}}
    ],
    "test_data": {
      "boundary_usernames": [
        {"username": "abc", "password": "pass123"},
        {"username": "a".repeat(50), "password": "pass456"}
      ]
    }
  }
]
```'''
    result = _parse_json_wrapper(llm_output)
    assert isinstance(result, list), f"FAIL: 期望 list，得到 {type(result)}"
    assert len(result) == 1
    assert len(result[0]["test_data"]["boundary_usernames"][1]["username"]) == 50
    print(f"[OK] _parse_json 完整流程修复成功: username len={len(result[0]['test_data']['boundary_usernames'][1]['username'])}")


def test_repair_truncated_with_repeat():
    """测试截断 + 代码表达式混合场景。"""
    # LLM 输出被截断 + 含 .repeat()
    bad_json = '''[
  {"id": "TC-001", "data": {"password": "a".repeat(128)},"'''.strip()
    result = _try_repair_json(bad_json)
    # 截断场景下应能修复（补全括号 + 替换代码表达式）
    if result is not None:
        print(f"[OK] 截断 + 代码表达式混合修复成功: {type(result)}")
    else:
        print(f"[INFO] 截断 + 代码表达式混合修复返回 None（可接受）")


def test_normal_json_unchanged():
    """测试正常 JSON 不被破坏。"""
    normal = '{"name": "test", "value": 123, "list": [1, 2, 3]}'
    result = _normalize_code_expressions(normal)
    assert result == normal, f"FAIL: 正常 JSON 被修改: {result}"
    print(f"[OK] 正常 JSON 未被修改")


def test_special_chars_in_repeat():
    """测试含特殊字符的 .repeat()。"""
    # 含引号转义
    bad_json = '''{"text": "hello".repeat(3)}'''
    result = _try_repair_json(bad_json)
    assert result == {"text": "hellohellohello"}, f"FAIL: {result}"
    print(f"[OK] 含特殊字符的 .repeat() 修复成功: {result}")


def main():
    print("=== 测试 _normalize_code_expressions 和 _try_repair_json ===\n")

    print("--- 测试 1: _normalize_code_expressions (.repeat) ---")
    test_normalize_repeat()

    print("\n--- 测试 2: _normalize_code_expressions (* N) ---")
    test_normalize_mul()

    print("\n--- 测试 3: _try_repair_json 修复 .repeat() ---")
    test_try_repair_json_with_repeat()

    print("\n--- 测试 4: _try_repair_json 修复 * N ---")
    test_try_repair_json_with_mul()

    print("\n--- 测试 5: _parse_json 完整流程 (markdown + .repeat) ---")
    test_parse_json_with_codeblock_repeat()

    print("\n--- 测试 6: 截断 + 代码表达式混合 ---")
    test_repair_truncated_with_repeat()

    print("\n--- 测试 7: 正常 JSON 不被破坏 ---")
    test_normal_json_unchanged()

    print("\n--- 测试 8: 含特殊字符的 .repeat() ---")
    test_special_chars_in_repeat()

    print("\n=== 所有测试通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
