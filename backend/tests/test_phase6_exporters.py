"""阶段6 导出器测试脚本。

测试覆盖（实现计划 V3 + 阶段6专项）：
    组1：Markdown 导出器（结构 / 字段缺失鲁棒性 / 步骤表格 / 追溯矩阵）
    组2：Gherkin 导出器（Feature/Scenario / Given-When-Then 关键词分配）
    组3：pytest 导出器（函数名生成 / 断言转换 / 语法正确性 / 装饰器）
    组4：工厂函数（格式分发 / MIME 类型 / 文件名 / 异常处理）
    组5：API 端点（通过 TestClient 验证四种格式端到端）

运行方式：python tests/test_phase6_exporters.py
（组1~4 是纯函数用例，也可直接被 pytest 收集；组5 需要真实 PG/Redis，
 标 integration，unit 层会 skip——见 tests/conftest.py 的分层策略）
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from unittest.mock import patch

import pytest

# 确保导入 backend 目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.exporters import export_cases, SUPPORTED_FORMATS
from app.exporters.markdown import export_markdown
from app.exporters.gherkin import export_gherkin
from app.exporters.pytest_code import (
    export_pytest,
    _make_func_name,
    _assertion_to_code,
    _repr_value,
)


# ============================================================================
# 颜色与结果统计
# ============================================================================

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印单个测试结果。"""
    results.append((name, ok, detail))
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    """打印分组标题。"""
    print(f"\n{YELLOW}━━ {title} ━━{RESET}")


# ============================================================================
# 测试夹具数据（真实 UTCF 结构，非模拟业务数据）
# ============================================================================

TEST_REQ_ID = "REQ-EXP-0001"
TEST_CASE_ID = "TC-EXP-0001-0001"

# 完整字段用例（覆盖所有可选项）
FULL_CASE = {
    "id": "TC-AUTH-0001-0001",
    "title": "正常登录流程验证",
    "type": "functional",
    "priority": "P0",
    "description": "验证标准登录流程",
    "tags": ["smoke", "regression"],
    "preconditions": ["测试账号 test_user 已注册", "账号未锁定"],
    "steps": [
        {"step": 1, "action": "打开登录页面", "expected": "页面正常加载"},
        {"step": 2, "action": "输入用户名 test_user", "expected": "输入框显示用户名"},
        {"step": 3, "action": "输入密码 Test@123456", "expected": "密码以密文显示"},
        {"step": 4, "action": "点击登录按钮", "expected": "登录成功，跳转首页"},
    ],
    "expected": "返回 token",
    "test_data": {"username": "test_user", "password": "Test@123456"},
    "assertions": [
        {"type": "status_code", "target": "response.status_code", "expected_value": 200, "message": "登录接口返回 200"},
        {"type": "not_null", "target": "response.token", "message": "Token 不为空"},
        {"type": "equals", "target": "response.redirect_url", "expected_value": "/home", "message": "跳转首页"},
    ],
    "cleanup": ["退出登录", "清理 Session"],
    "related_requirement": "REQ-2024-001",
    "source_pattern": "auth_pattern",
    "confidence": 0.95,
    "execution": {"timeout_seconds": 15, "retry_count": 0, "depends_on": []},
}

# 字段缺失的最简用例（仅必填字段）
MINIMAL_CASE = {
    "id": "TC-VAL-0001-0001",
    "title": "密码长度边界值测试",
    "type": "boundary",
    "priority": "P2",
    "steps": [
        {"action": "输入 5 位密码", "expected": "提示密码过短"},
        {"action": "输入 6 位密码", "expected": "允许提交"},
    ],
    "related_requirement": "REQ-2024-001",
}

# 含所有断言类型的用例（验证断言转换完整性）
ALL_ASSERTION_CASE = {
    "id": "TC-SEC-0001-0001",
    "title": "断言类型完整性测试",
    "type": "security",
    "priority": "P1",
    "steps": [{"step": 1, "action": "发起请求", "expected": "返回响应"}],
    "related_requirement": "REQ-2024-001",
    "assertions": [
        {"type": "equals", "target": "response.code", "expected_value": 200},
        {"type": "status_code", "target": "response.status_code", "expected_value": 403},
        {"type": "not_null", "target": "response.token"},
        {"type": "contains", "target": "response.message", "expected_value": "success"},
        {"type": "gt", "target": "response.count", "expected_value": 0},
        {"type": "lt", "target": "response.retry", "expected_value": 5},
        {"type": "matches", "target": "response.email", "expected_value": r"^\S+@\S+$"},
        {"type": "custom", "target": "", "message": "自定义断言需手工补充"},
    ],
}


# ============================================================================
# 组1：Markdown 导出器
# ============================================================================

def test_markdown_exporter() -> None:
    section("组1：Markdown 导出器")

    # 1.1 空用例列表
    md = export_markdown([])
    check("空列表返回非空文本", len(md) > 0)
    check("空列表含提示", "无可导出的用例" in md)

    # 1.2 单个完整用例
    md = export_markdown([FULL_CASE])
    check("含文档标题", "# 测试用例文档" in md)
    check("含覆盖率概览", "## 覆盖率概览" in md)
    check("含功能测试用例分组", "功能测试用例" in md)
    check("含用例 ID", "TC-AUTH-0001-0001" in md)
    check("含用例标题", "正常登录流程验证" in md)
    check("含优先级", "P0" in md)
    check("含标签", "`smoke`" in md and "`regression`" in md)
    check("含追溯需求", "REQ-2024-001" in md)
    check("含置信度", "95%" in md)
    check("含参考模式", "auth_pattern" in md)
    check("含前置条件", "测试账号 test_user 已注册" in md)
    check("含步骤表头", "| 步骤 | 操作 | 预期结果 |" in md)
    check("含步骤1", "打开登录页面" in md)
    check("含步骤4", "点击登录按钮" in md)
    check("含整体预期", "返回 token" in md)
    check("含测试数据 JSON", "```json" in md)
    check("含断言", "status_code" in md)
    check("含清理操作", "退出登录" in md)
    check("含执行配置", "超时" in md and "15 秒" in md)
    check("含追溯矩阵", "## 追溯矩阵" in md)
    check("含 AI 备注", "AI 生成备注" in md)

    # 1.3 字段缺失用例（鲁棒性）
    md = export_markdown([MINIMAL_CASE])
    check("最简用例含标题", "# 测试用例文档" in md)
    check("最简用例含 ID", "TC-VAL-0001-0001" in md)
    check("最简用例含步骤", "输入 5 位密码" in md)
    check("最简用例无前置条件（不报错）", "前置条件" not in md or "**前置条件:**" not in md)
    check("最简用例无清理操作", "清理操作" not in md)

    # 1.4 多用例按类型分组（v3.5.11：taxonomy 分组，boundary 归功能组）
    md = export_markdown([FULL_CASE, MINIMAL_CASE])
    check("多类型分组含功能测试", "功能测试用例" in md)
    check("边界用例归入功能组（复合标签保留细分）", "功能测试·边界值" in md)
    check("覆盖率总计为 2", "| **总计** | **2** |" in md)

    # 1.5 表格竖线转义
    pipe_case = {
        "id": "TC-PIPE-0001-0001",
        "title": "竖线测试",
        "type": "functional",
        "priority": "P1",
        "steps": [{"step": 1, "action": "输入 a|b", "expected": "显示 a|b"}],
        "related_requirement": "REQ-X",
    }
    md = export_markdown([pipe_case])
    check("竖线被转义", "a\\|b" in md)


# ============================================================================
# 组2：Gherkin 导出器
# ============================================================================

def test_gherkin_exporter() -> None:
    section("组2：Gherkin 导出器")

    # 2.1 空用例列表
    gk = export_gherkin([])
    check("空列表含 Feature", gk.startswith("Feature:"))
    check("空列表含提示", "无可导出的用例" in gk)

    # 2.2 单个完整用例
    gk = export_gherkin([FULL_CASE])
    lines = gk.split("\n")
    check("首行是 Feature", lines[0].startswith("Feature:"))
    check("Feature 含需求 ID", "REQ-2024-001" in lines[0])
    check("含 Scenario", "Scenario:" in gk)
    check("含用例 ID 在 Scenario", "[TC-AUTH-0001-0001]" in gk)
    check("含优先级注释", "# Priority: P0" in gk)
    check("含类型注释", "Type: functional" in gk)
    check("含 Given 前置条件", "Given 测试账号 test_user 已注册" in gk)
    check("含 When 步骤", "When 打开登录页面" in gk)
    check("含 Then 预期", "Then 页面正常加载" in gk)

    # 2.3 关键词分配规则：无前置条件时首步骤用 Given
    gk_no_pc = export_gherkin([MINIMAL_CASE])
    # MINIMAL_CASE 无 preconditions，首步骤 action 应为 Given
    check("无前置条件时首步骤用 Given", "Given 输入 5 位密码" in gk_no_pc)
    check("无前置条件时首步骤预期用 Then", "Then 提示密码过短" in gk_no_pc)

    # 2.4 多需求用例：Feature 标题回退为"测试用例集"
    multi_req_case = {
        "id": "TC-X-0001-0001",
        "title": "X",
        "type": "functional",
        "priority": "P1",
        "steps": [{"step": 1, "action": "x", "expected": "y"}],
        "related_requirement": "REQ-OTHER",
    }
    gk = export_gherkin([FULL_CASE, multi_req_case])
    check("多需求 Feature 标题回退", "Feature: 测试用例集" in gk)
    check("多需求含两个 Scenario", gk.count("Scenario:") == 2)


# ============================================================================
# 组3：pytest 导出器
# ============================================================================

def test_pytest_exporter() -> None:
    section("组3：pytest 导出器")

    # 3.1 空用例列表
    py = export_pytest([])
    check("空列表含 import pytest", "import pytest" in py)
    check("空列表含提示", "无可导出的用例" in py)

    # 3.2 单个完整用例
    py = export_pytest([FULL_CASE])
    check("含文件头 docstring", '"""自动生成的 pytest 测试文件' in py)
    check("含 import pytest", "import pytest" in py)
    check("含 @pytest.mark.functional", "@pytest.mark.functional" in py)
    check("含 @pytest.mark.smoke", "@pytest.mark.smoke" in py)
    check("含函数定义", "def test_tc_auth_0001_0001():" in py)
    check("含 docstring 标题", "正常登录流程验证" in py)
    check("含优先级元信息", "Priority: P0" in py)
    check("含 Given 前置条件注释", "# Given: 测试账号 test_user 已注册" in py)
    check("含步骤注释", "# Step 1: 打开登录页面" in py)
    check("含预期注释", "# Expected: 页面正常加载" in py)
    check("含 status_code 断言", "assert response.status_code == 200" in py)
    check("含 not_null 断言", "assert response.token is not None" in py)
    check("含 equals 断言", 'assert response.redirect_url == "/home"' in py)
    check("含断言说明注释", "# 登录接口返回 200" in py)

    # 3.3 关键：生成的 Python 代码语法正确
    try:
        ast.parse(py)
        check("生成的 pytest 代码语法正确", True)
    except SyntaxError as e:
        check("生成的 pytest 代码语法正确", False, f"SyntaxError: {e}")

    # 3.4 断言类型转换完整性
    py_all = export_pytest([ALL_ASSERTION_CASE])
    check("equals 转换", "assert response.code == 200" in py_all)
    check("status_code 转换", "assert response.status_code == 403" in py_all)
    check("not_null 转换", "assert response.token is not None" in py_all)
    check("contains 转换", '"success" in response.message' in py_all)
    check("gt 转换", "assert response.count > 0" in py_all)
    check("lt 转换", "assert response.retry < 5" in py_all)
    check("matches 转换 + import re", "import re" in py_all)
    check("matches 调用 re.search", "re.search(" in py_all)
    check("custom 转注释", "# Custom Assertion: 自定义断言需手工补充" in py_all)

    # 3.5 语法正确性（含所有断言）
    try:
        ast.parse(py_all)
        check("全断言 pytest 代码语法正确", True)
    except SyntaxError as e:
        check("全断言 pytest 代码语法正确", False, f"SyntaxError: {e}")

    # 3.6 函数名兜底（pass 语句）
    no_assert_case = {
        "id": "TC-NON-0001-0001",
        "title": "无断言用例",
        "type": "functional",
        "priority": "P2",
        "steps": [{"step": 1, "action": "x", "expected": "y"}],
        "related_requirement": "REQ-X",
    }
    py = export_pytest([no_assert_case])
    check("无断言时含 pass 兜底", "pass  # TODO: 补充实际操作与断言" in py)
    try:
        ast.parse(py)
        check("无断言 pytest 代码语法正确", True)
    except SyntaxError as e:
        check("无断言 pytest 代码语法正确", False, f"SyntaxError: {e}")


# ============================================================================
# 组3.7：工具函数单元测试
# ============================================================================

def test_pytest_utils() -> None:
    section("组3.7：pytest 导出器工具函数")

    # _make_func_name
    used: set[str] = set()
    name1 = _make_func_name("TC-AUTH-0001-0001", used)
    check("case_id 转函数名", name1 == "test_tc_auth_0001_0001")
    name2 = _make_func_name("TC-AUTH-0001-0001", used)
    check("重复 case_id 加后缀", name2 == "test_tc_auth_0001_0001_2", name2)
    name3 = _make_func_name("", used)
    check("空 case_id 回退 test_case", name3 == "test_case")
    name4 = _make_func_name("123abc", used)
    check("数字开头加 test_ 前缀", name4.startswith("test_"))

    # _assertion_to_code
    check("equals 断言代码",
          _assertion_to_code({"type": "equals", "target": "x", "expected_value": 1}) == 'assert x == 1')
    check("status_code 断言代码",
          _assertion_to_code({"type": "status_code", "target": "x", "expected_value": 200}) == "assert x == 200")
    check("not_null 断言代码",
          _assertion_to_code({"type": "not_null", "target": "x"}) == "assert x is not None")
    check("contains 断言代码",
          _assertion_to_code({"type": "contains", "target": "x", "expected_value": "ab"}) == 'assert "ab" in x')
    check("gt 断言代码",
          _assertion_to_code({"type": "gt", "target": "x", "expected_value": 5}) == "assert x > 5")
    check("lt 断言代码",
          _assertion_to_code({"type": "lt", "target": "x", "expected_value": 5}) == "assert x < 5")
    check("matches 断言代码",
          "re.search" in _assertion_to_code({"type": "matches", "target": "x", "expected_value": "^a$"}))
    check("custom 断言返回空",
          _assertion_to_code({"type": "custom", "message": "x"}) == "")
    check("target 空用 response 占位",
          "response" in _assertion_to_code({"type": "not_null", "target": ""}))

    # _repr_value
    check("None 表示", _repr_value(None) == "None")
    check("字符串表示", _repr_value("abc") == '"abc"')
    check("含引号字符串转义", _repr_value('a"b') == '"a\\"b"')
    check("整数表示", _repr_value(42) == "42")
    check("浮点表示", _repr_value(3.14) == "3.14")
    check("True 表示", _repr_value(True) == "True")
    check("False 表示", _repr_value(False) == "False")
    check("列表表示", _repr_value([1, 2]) == "[1, 2]")


# ============================================================================
# 组4：工厂函数
# ============================================================================

def test_factory() -> None:
    section("组4：工厂函数 export_cases")

    cases = [FULL_CASE, MINIMAL_CASE]

    # 4.1 各格式返回正确的 MIME 和文件名
    for fmt, expected_mime, expected_ext in [
        ("markdown", "text/markdown", ".md"),
        ("gherkin", "text/plain", ".feature"),
        ("pytest", "text/x-python", ".py"),
        ("json", "application/json", ".json"),
    ]:
        content, mime, filename = export_cases(cases, fmt)
        check(f"{fmt} 返回非空内容", len(content) > 0)
        check(f"{fmt} MIME 正确", expected_mime in mime, mime)
        check(f"{fmt} 文件名正确", filename.endswith(expected_ext), filename)

    # 4.2 不支持的格式
    try:
        export_cases(cases, "xml")
        check("不支持的格式抛 ValueError", False)
    except ValueError as e:
        check("不支持的格式抛 ValueError", "xml" in str(e))

    # 4.3 SUPPORTED_FORMATS 完整性
    check("SUPPORTED_FORMATS 含 6 种格式", len(SUPPORTED_FORMATS) == 6)
    for fmt in ("json", "markdown", "gherkin", "pytest", "xmind", "xmind8"):
        check(f"SUPPORTED_FORMATS 含 {fmt}", fmt in SUPPORTED_FORMATS)


# ============================================================================
# 组5：API 端点端到端
# ============================================================================

TEST_SRR = {
    "srr_version": "1.0",
    "requirement": {
        "id": TEST_REQ_ID,
        "title": "导出测试需求",
        "summary": "用于测试导出",
        "product_id": "PROD-EXP",
        "priority": "P2",
        "modules": ["test"],
        "change_type": "new",
    },
    "flows": {"happy_path": {"steps": []}, "alternate_flows": []},
    "preconditions": [],
    "business_rules": [],
    "data_constraints": [],
    "test_points_hint": [],
}

TEST_CASE_JSON = {
    "id": TEST_CASE_ID,
    "title": "导出测试用例",
    "type": "functional",
    "priority": "P1",
    "steps": [{"step": 1, "action": "执行操作", "expected": "返回成功"}],
    "expected": "成功",
    "related_requirement": TEST_REQ_ID,
    "confidence": 0.88,
}


async def setup_fixtures() -> None:
    """插入测试夹具数据到 DB。"""
    from sqlalchemy import delete

    from app.db.database import async_session
    from app.db.models import Requirement, TestCase
    async with async_session() as session:
        # 先清理残留
        await session.execute(delete(TestCase).where(TestCase.req_id == TEST_REQ_ID))
        await session.execute(delete(Requirement).where(Requirement.id == TEST_REQ_ID))
        await session.commit()

    async with async_session() as session:
        session.add(Requirement(
            id=TEST_REQ_ID,
            product_id="PROD-EXP",
            title="导出测试需求",
            priority="P2",
            module="test",
            modules=["test"],
            status="active",
            srr_json=TEST_SRR,
            raw_text="导出测试需求",
            source_type="markdown",
            created_by="test",
        ))
        session.add(TestCase(
            id=TEST_CASE_ID,
            req_id=TEST_REQ_ID,
            product_id="PROD-EXP",
            title="导出测试用例",
            type="functional",
            priority="P1",
            status="draft",
            case_json=TEST_CASE_JSON,
            source="ai_generated",
            gen_batch_id="BATCH-EXP-001",
            confidence=0.88,
        ))
        await session.commit()


async def cleanup_fixtures() -> None:
    """清理测试夹具数据。"""
    from sqlalchemy import delete

    from app.db.database import async_session
    from app.db.models import Requirement, TestCase
    async with async_session() as session:
        await session.execute(delete(TestCase).where(TestCase.req_id == TEST_REQ_ID))
        await session.execute(delete(Requirement).where(Requirement.id == TEST_REQ_ID))
        await session.commit()


@pytest.mark.integration
def test_api_endpoint(client) -> None:
    """组5：导出 API 端到端（需要真实 PG/Redis，unit 层 skip）。"""
    from app.core.auth import get_default_token

    section("组5：API 端点端到端")

    headers = {"Authorization": f"Bearer {get_default_token()}"}

    # 5.1 不存在的用例 → 404
    resp = client.post("/api/export", json={"format": "markdown", "case_ids": ["TC-NOT-EXIST"]}, headers=headers)
    check("导出不存在的用例返回 404", resp.status_code == 404)

    # 5.2 JSON 端到端
    resp = client.post("/api/export", json={"format": "json", "case_ids": [TEST_CASE_ID]}, headers=headers)
    check("API JSON 导出 200", resp.status_code == 200, f"status={resp.status_code}")
    check("API JSON Content-Type", "application/json" in resp.headers.get("content-type", ""))
    check("API JSON 含 attachment 头", "attachment" in resp.headers.get("content-disposition", ""))
    check("API JSON 文件名正确", "testcases.json" in resp.headers.get("content-disposition", ""))
    try:
        cases = resp.json()
        check("API JSON 可解析为数组", isinstance(cases, list))
        check("API JSON 含测试用例", any(c.get("id") == TEST_CASE_ID for c in cases))
    except Exception as e:
        check("API JSON 可解析", False, str(e))

    # 5.3 Markdown 端到端
    resp = client.post("/api/export", json={"format": "markdown", "case_ids": [TEST_CASE_ID]}, headers=headers)
    check("API Markdown 导出 200", resp.status_code == 200, f"status={resp.status_code}")
    check("API Markdown Content-Type", "text/markdown" in resp.headers.get("content-type", ""))
    check("API Markdown 文件名正确", "testcases.md" in resp.headers.get("content-disposition", ""))
    check("API Markdown 含测试用例 ID", TEST_CASE_ID in resp.text)

    # 5.4 Gherkin 端到端
    resp = client.post("/api/export", json={"format": "gherkin", "case_ids": [TEST_CASE_ID]}, headers=headers)
    check("API Gherkin 导出 200", resp.status_code == 200, f"status={resp.status_code}")
    check("API Gherkin Content-Type", "text/plain" in resp.headers.get("content-type", ""))
    check("API Gherkin 文件名正确", "testcases.feature" in resp.headers.get("content-disposition", ""))
    check("API Gherkin 含 Feature", resp.text.startswith("Feature:"))
    check("API Gherkin 含 Scenario", "Scenario:" in resp.text)

    # 5.5 pytest 端到端
    resp = client.post("/api/export", json={"format": "pytest", "case_ids": [TEST_CASE_ID]}, headers=headers)
    check("API pytest 导出 200", resp.status_code == 200, f"status={resp.status_code}")
    check("API pytest Content-Type", "text/x-python" in resp.headers.get("content-type", ""))
    check("API pytest 文件名正确", "testcases.py" in resp.headers.get("content-disposition", ""))
    check("API pytest 含 import pytest", "import pytest" in resp.text)
    check("API pytest 含测试函数", "def test_tc_" in resp.text)
    # 验证生成的代码语法正确
    try:
        ast.parse(resp.text)
        check("API pytest 代码语法正确", True)
    except SyntaxError as e:
        check("API pytest 代码语法正确", False, str(e))

    # 5.6 指定 case_ids 导出
    resp = client.post("/api/export", json={"format": "markdown", "case_ids": [TEST_CASE_ID]}, headers=headers)
    check("API 指定 case_ids 导出 200", resp.status_code == 200)
    check("API 指定 case_ids 含目标用例", TEST_CASE_ID in resp.text)


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    print(f"{YELLOW}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{YELLOW}║  阶段6 导出器测试                                              ║{RESET}")
    print(f"{YELLOW}╚══════════════════════════════════════════════════════════════╝{RESET}")

    # 初始化 DB
    from app.db.database import init_db, engine

    async def _init() -> None:
        await init_db()
        await engine.dispose(close=False)

    async def _setup() -> None:
        await setup_fixtures()
        await engine.dispose(close=False)

    async def _cleanup() -> None:
        await engine.dispose(close=False)
        await cleanup_fixtures()
        await engine.dispose(close=False)

    asyncio.run(_init())

    # 插入夹具
    asyncio.run(_setup())

    try:
        # 直接测试导出器（无需 TestClient）
        test_markdown_exporter()
        test_gherkin_exporter()
        test_pytest_exporter()
        test_pytest_utils()
        test_factory()

        # 通过 TestClient 测试 API 端点
        from fastapi.testclient import TestClient

        from app.main import app
        with TestClient(app) as client:
            test_api_endpoint(client)

    finally:
        asyncio.run(_cleanup())

    # 汇总
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed

    print(f"\n{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    if failed == 0:
        print(f"{GREEN}✓ 全部 {total} 项测试通过{RESET}")
    else:
        print(f"{RED}✗ {failed}/{total} 项测试失败：{RESET}")
        for name, ok, detail in results:
            if not ok:
                print(f"  {RED}✗{RESET} {name} — {detail}")
    print(f"{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
