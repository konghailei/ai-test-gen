"""pytest 导出器。

将 UTCF 测试用例列表转换为 pytest 测试文件，用于：
- 自动化测试执行
- CI/CD 集成
- 单元测试框架对接

设计依据：docs/04-output-format.md §4.1（pytest 适配器）

输出结构：
    import pytest
    # 文件头注释

    @pytest.mark.functional
    def test_tc_auth_0001_0001():
        \"\"\"正常登录流程验证\"\"\"
        # Given: 测试账号已注册
        # Step 1: 打开登录页面
        # Expected: 页面正常加载
        # Step 2: 输入用户名
        # Expected: 输入框显示用户名
        # 断言：response.status_code == 200
        assert response.status_code == 200
        ...

断言生成规则：
    - equals / status_code → assert {target} == {expected_value}
    - not_null → assert {target} is not None
    - contains → assert {expected_value} in {target}
    - gt / lt → assert {target} > / < {expected_value}
    - matches → assert re.search({expected_value}, {target})  （含 import re）
    - custom → 以注释形式给出（无法自动生成可执行代码）
"""

from __future__ import annotations

import keyword
import re

from app.utils.logger import logger


# ============================================================================
# 文本转义工具函数
# 防止 LLM/用户输入通过 docstring 或注释拼接注入任意代码：
# - docstring 中的 """ 会提前结束字符串，使后续内容被当作可执行代码
# - 注释中的换行符会使后续行脱离 # 前缀，被解析为代码
# ============================================================================

def _sanitize_docstring(text) -> str:
    """转义三引号，用于 docstring 拼接，防止提前结束 docstring 导致代码注入。

    Args:
        text: 待拼入 docstring 的原始文本（可能来自 LLM/用户输入）

    Returns:
        str: 转义后的安全文本（三引号会被转义为反斜杠加引号的形式）
    """
    if not text:
        return ""
    # 先转义反斜杠，避免其与后续转义序列冲突
    s = str(text).replace("\\", "\\\\")
    # 转义三引号，使解析器不会将其视为 docstring 结束符
    return s.replace('"""', '\\"\\"\\"')


def _sanitize_comment(text) -> str:
    """单行化文本，用于 # 注释拼接，防止换行符导致后续行脱离注释前缀被当作代码执行。

    Args:
        text: 待拼入 # 注释的原始文本（可能来自 LLM/用户输入）

    Returns:
        str: 单行化后的文本（\\n/\\r → 空格）
    """
    if not text:
        return ""
    return str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


# ============================================================================
# 主导出函数
# ============================================================================

def export_pytest(cases: list[dict]) -> str:
    """将用例列表导出为 pytest 测试文件。

    Args:
        cases: 用例 JSON 列表，每项为单条 UTCF TestCase dict

    Returns:
        str: Python 测试文件内容（.py）

    Note:
        - 文件头含 import pytest（断言类型含 matches 时追加 import re）
        - 每个用例转为一个测试函数，函数名由 case_id 转换而来
        - 函数体含 docstring、前置条件注释、步骤注释、断言语句
        - 字段缺失时跳过对应部分，不抛异常
        - 每个函数末尾自动追加 `pass` 兜底（避免空函数体导致语法错误）
    """
    if not cases:
        return _file_header([], 0) + "\n# 无可导出的用例\n"

    # 预扫描：是否需要 import re（matches 类型断言）
    need_re_import = any(
        any(a.get("type") == "matches" for a in (c.get("assertions") or []))
        for c in cases
    )

    lines: list[str] = []
    lines.append(_file_header(cases, len(cases), need_re_import))

    # ========================================================================
    # 每个用例 → 一个测试函数
    # ========================================================================
    used_func_names: set[str] = set()
    for case in cases:
        func_code = _case_to_function(case, used_func_names)
        lines.append(func_code)
        lines.append("")

    logger.debug("[Exporter/pytest] 生成完成: cases={}", len(cases))
    return "\n".join(lines)


# ============================================================================
# 文件头生成
# ============================================================================

def _file_header(cases: list[dict], total: int, need_re_import: bool = False) -> str:
    """生成文件头（注释 + import）。

    Args:
        cases: 用例列表（用于提取需求信息）
        total: 用例总数
        need_re_import: 是否需要 import re（matches 断言）

    Returns:
        str: 文件头文本（含末尾换行）
    """
    lines: list[str] = []
    lines.append('"""自动生成的 pytest 测试文件。')
    lines.append("")
    lines.append(f"由 AI测试用例生成系统 导出，共 {total} 个测试用例。")
    req_ids = {c.get("related_requirement") for c in cases if c.get("related_requirement")}
    if len(req_ids) == 1:
        lines.append(f"关联需求: {_sanitize_docstring(next(iter(req_ids)))}")
    elif len(req_ids) > 1:
        safe_ids = ", ".join(_sanitize_docstring(rid) for rid in sorted(req_ids))
        lines.append(f"关联需求: {safe_ids}")
    lines.append("")
    lines.append("注意：")
    lines.append("- 步骤以注释形式呈现，需测试人员补充实际操作代码")
    lines.append("- 断言已生成可执行代码，但 target 路径需根据实际响应对象调整")
    lines.append('- 用例失败时请标注 actual vs expected')
    lines.append('"""')
    lines.append("")
    lines.append("import pytest")
    if need_re_import:
        lines.append("import re")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


# ============================================================================
# 单个用例 → 测试函数
# ============================================================================

def _case_to_function(case: dict, used_names: set[str]) -> str:
    """将单个用例转换为 pytest 测试函数代码。

    Args:
        case: 单条用例 dict
        used_names: 已使用的函数名集合（用于去重，避免函数名冲突）

    Returns:
        str: 函数代码文本（不含末尾换行）
    """
    func_name = _make_func_name(case.get("id", "unknown"), used_names)
    title = case.get("title", "")
    priority = case.get("priority", "")
    typ = case.get("type", "unknown")
    tags = case.get("tags") or []

    lines: list[str] = []

    # 装饰器：@pytest.mark.{type}（按 docs/04 §4.1）
    if typ:
        lines.append(f"@pytest.mark.{typ}")

    # 额外的标签装饰器（如 smoke / regression）
    for tag in tags:
        # 仅保留合法标识符作为 mark
        safe_tag = re.sub(r"[^a-zA-Z0-9_]", "_", tag)
        if safe_tag and not keyword.iskeyword(safe_tag):
            lines.append(f"@pytest.mark.{safe_tag}")

    # 函数定义
    lines.append(f"def {func_name}():")
    # docstring（含标题和元信息）
    doc_lines = [_sanitize_docstring(title)] if title else []
    meta_parts = []
    if priority:
        meta_parts.append(f"Priority: {priority}")
    if typ:
        meta_parts.append(f"Type: {typ}")
    if case.get("related_requirement"):
        meta_parts.append(f"Req: {_sanitize_docstring(case['related_requirement'])}")
    if meta_parts:
        doc_lines.append("")
        doc_lines.append(" | ".join(meta_parts))
    if case.get("description"):
        doc_lines.append("")
        doc_lines.append(_sanitize_docstring(case["description"]))

    if doc_lines:
        lines.append('    """')
        for dl in doc_lines:
            lines.append(f"    {dl}" if dl else "    ")
        lines.append('    """')

    # 前置条件 → # Given: 注释
    preconditions = case.get("preconditions") or []
    for pc in preconditions:
        lines.append(f"    # Given: {_sanitize_comment(pc)}")

    # 步骤 → # Step N / # Expected 注释
    steps = case.get("steps") or []
    for idx, step in enumerate(steps, start=1):
        step_no = step.get("step", idx)
        action = step.get("action", "")
        expected = step.get("expected", "")
        lines.append(f"    # Step {step_no}: {_sanitize_comment(action)}")
        if expected:
            lines.append(f"    # Expected: {_sanitize_comment(expected)}")

    # 整体预期（可选）
    overall_expected = case.get("expected")
    if overall_expected:
        lines.append(f"    # Overall Expected: {_sanitize_comment(overall_expected)}")

    # 断言 → 可执行的 assert 语句
    assertions = case.get("assertions") or []
    has_executable_assert = False
    for assertion in assertions:
        code = _assertion_to_code(assertion)
        if code:
            lines.append(f"    {code}")
            has_executable_assert = True
        else:
            # custom 类型无法自动生成代码，以注释形式给出
            message = assertion.get("message", "")
            lines.append(f"    # Custom Assertion: {_sanitize_comment(message)}")

    # 函数体兜底：若无可执行断言，添加 pass 避免空函数体
    if not has_executable_assert:
        lines.append("    pass  # TODO: 补充实际操作与断言")

    return "\n".join(lines)


# ============================================================================
# 工具函数
# ============================================================================

def _make_func_name(case_id: str, used_names: set[str]) -> str:
    """从 case_id 生成合法的 Python 函数名。

    转换规则（docs/04 §4.1）：
        TC-AUTH-0001-0001 → test_tc_auth_0001_0001

    Args:
        case_id: 用例 ID
        used_names: 已使用的函数名集合（用于去重）

    Returns:
        str: 合法的 Python 函数名（全小写 + 下划线）

    Note:
        - 若 case_id 为空或非法，回退为 test_case_N
        - 若函数名冲突，追加 _2 / _3 等后缀
    """
    if not case_id:
        base = "test_case"
    else:
        # 小写 + 连字符转下划线
        base = case_id.lower().replace("-", "_")
        # 移除其他非法字符
        base = re.sub(r"[^a-zA-Z0-9_]", "_", base)
        # 确保不以数字开头
        if base and base[0].isdigit():
            base = f"test_{base}"
        # pytest 默认只收集 test_ 开头的函数，强制加前缀
        if not base.startswith("test_"):
            base = f"test_{base}"

    if not base:
        base = "test_case"

    # 函数名去重
    func_name = base
    suffix = 2
    while func_name in used_names:
        func_name = f"{base}_{suffix}"
        suffix += 1
    used_names.add(func_name)
    return func_name


# target 表达式白名单：仅允许标识符链 + 属性访问 + 无参方法调用 + 字面量索引。
# 防止 LLM/用户通过 assertion.target 注入任意 Python 代码
# （如 `True or __import__('os').system('rm -rf /')` 会被拼入 assert 语句执行）。
_SAFE_TARGET_RE = re.compile(
    r'^[a-zA-Z][a-zA-Z0-9_]*'  # 基础标识符（必须以字母开头，阻断 __import__ 等）
    r'(?:'
    r'\.[a-zA-Z][a-zA-Z0-9_]*'  # .attr 属性访问（必须以字母开头，阻断 .__class__ 等 dunder 逃逸）
    r'|\(\)'                      # .method() 无参方法调用（如 response.json()）
    r'|\[\s*(?:\d+|"[^"\n]*"|\'[^\'\n]*\')\s*\]'  # [int] / ["str"] / ['str'] 索引
    r')*$'
)


def _sanitize_target(target: str) -> str | None:
    """校验断言 target 表达式，防止代码注入。

    target 来自 LLM 生成或用户输入的测试用例数据，会被直接拼入 `assert` 语句。
    仅放行安全的属性访问/索引表达式（如 response.status_code、response.json()["data"]），
    拒绝包含函数调用参数、运算符、分号等可执行代码的形式。

    Args:
        target: 待校验的 target 表达式字符串

    Returns:
        str | None: 空字符串原样返回（由调用方回退为 response 占位符）；
        非空且通过白名单返回原 target；不通过返回 None（调用方应跳过自动生成）
    """
    if not target:
        return target  # 空字符串由调用方回退为 response 占位符
    if _SAFE_TARGET_RE.match(target):
        return target
    logger.warning(
        "[exporter/pytest] assertion target 校验未通过，已跳过自动生成断言: len={}",
        len(target),
    )
    return None


def _assertion_to_code(assertion: dict) -> str:
    """将单条断言转为可执行的 assert 语句。

    Args:
        assertion: 断言 dict，含 type/target/expected_value/message

    Returns:
        str: assert 语句字符串；custom 类型或 target 校验失败返回空字符串（由调用方以注释形式处理）

    断言类型映射（docs/04 §1 + §4.1）：
        - equals / status_code → assert {target} == {value}
        - not_null → assert {target} is not None
        - contains → assert {value} in {target}
        - gt → assert {target} > {value}
        - lt → assert {target} < {value}
        - matches → assert re.search({value}, {target})
        - custom → 无法自动生成，返回 ""
    """
    a_type = assertion.get("type", "")
    target = assertion.get("target", "")
    expected_value = assertion.get("expected_value")
    message = assertion.get("message", "")

    # target 白名单校验，防止通过 target 注入任意代码（如 __import__('os').system(...)）。
    # 校验失败返回 ""，由调用方以注释形式提示，不生成可执行 assert。
    sanitized = _sanitize_target(target)
    if sanitized is None:
        return ""
    # target 为空时用占位符 response（提示用户替换）
    target_expr = sanitized if sanitized else "response"

    # 构造 assert 表达式
    if a_type in ("equals", "status_code"):
        value_repr = _repr_value(expected_value)
        expr = f"assert {target_expr} == {value_repr}"
    elif a_type == "not_null":
        expr = f"assert {target_expr} is not None"
    elif a_type == "contains":
        value_repr = _repr_value(expected_value)
        # contains：expected_value 在 target 中
        expr = f"assert {value_repr} in {target_expr}"
    elif a_type == "gt":
        value_repr = _repr_value(expected_value)
        expr = f"assert {target_expr} > {value_repr}"
    elif a_type == "lt":
        value_repr = _repr_value(expected_value)
        expr = f"assert {target_expr} < {value_repr}"
    elif a_type == "matches":
        value_repr = _repr_value(expected_value)
        expr = f"assert re.search({value_repr}, str({target_expr}))"
    else:
        # custom 或未知类型：无法自动生成可执行代码
        return ""

    # 追加断言说明注释（若有）
    if message:
        expr += f"  # {_sanitize_comment(message)}"
    return expr


def _repr_value(value) -> str:
    """将 Python 值转为代码字面量表示。

    Args:
        value: 任意值（str/int/float/bool/None/list/dict）

    Returns:
        str: 该值的 Python 字面量（用于 assert 语句）
            - 字符串 → 加引号
            - None → "None"
            - bool → "True"/"False"
            - 数字 → str(value)
            - list/dict → repr(value)
    """
    if value is None:
        return "None"
    if isinstance(value, str):
        # 转义反斜杠、引号和控制字符，防止换行符导致 SyntaxError 或代码注入
        escaped = (value.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                        .replace("\r", "\\r")
                        .replace("\t", "\\t"))
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    # list / dict / 其他
    return repr(value)
