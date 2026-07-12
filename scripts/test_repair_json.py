"""验证 _try_repair_json 对截断 JSON 的修复能力。"""
import json
import sys
sys.path.insert(0, "backend")

from app.core.llm_client import _try_repair_json, _extract_json_block

# 模拟 LLM 退化截断场景
truncated_json = '{"test_suite": [{"id": "TC-001", "title": "用户注册", "type": "functional", "priority": "P0", "steps": [{"action": "打开注册页", "expected": "显示注册表单"}]}, {"id": "TC-002", "title": "邮箱验证", "type": "functional", "priority": "P0", "description": "aaaaaaaaaaaa'

print(f"输入长度: {len(truncated_json)}")
print(f"输入末尾: ...{truncated_json[-60:]}")
print()

result = _try_repair_json(truncated_json)
if result:
    print(f"[OK] 修复成功!")
    print(f"  test_suite 数量: {len(result.get('test_suite', []))}")
    for i, tc in enumerate(result.get("test_suite", []), 1):
        print(f"  [{i}] id={tc.get('id')} title={tc.get('title', '')[:40]}")
else:
    print("[FAIL] 修复失败")

# 测试更复杂的嵌套截断
truncated2 = '{"test_suite": [{"id": "TC-001", "title": "登录", "steps": [{"action": "输入用户名", "expected": "显示"}, {"action": "输入密码", "expected": "显示密码'
print(f"\n测试2 - 嵌套数组截断:")
result2 = _try_repair_json(truncated2)
if result2:
    print(f"[OK] 修复成功! test_suite={len(result2.get('test_suite', []))}")
else:
    print("[FAIL] 修复失败")

# 测试正常 JSON（不应修改）
normal = '{"test_suite": [{"id": "TC-001"}]}'
result3 = _try_repair_json(normal)
print(f"\n测试3 - 正常JSON: {'OK' if result3 else 'FAIL'}")
