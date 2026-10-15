"""生成、审核与入库共用的用例语义合同，不修补缺失的测试内容。"""

from __future__ import annotations

import re
from typing import Any


ASSERTION_TYPE_MAP = {
    "equals": "equals", "contains": "contains", "matches": "matches",
    "status_code": "status_code", "gt": "gt", "lt": "lt",
    "not_null": "not_null", "custom": "custom",
    "equal": "equals", "equal_to": "equals", "==": "equals", "=": "equals",
    "eq": "equals", "greater": "gt", "greater_than": "gt", ">": "gt",
    "less": "lt", "less_than": "lt", "<": "lt", "regex": "matches",
    "exists": "not_null", "contains_text": "contains", "in": "contains",
    "not_equals": "custom", "not_equal": "custom", "not_contains": "custom",
    "not_contain": "custom", "not_exists": "custom", "not_in": "custom",
    # UTCF 没有非严格比较枚举，保留操作符含义，不能改写为严格大于/小于。
    "greater_or_equal": "custom", "less_or_equal": "custom",
}


class GeneratedCaseValidationError(ValueError):
    """模型返回的用例不满足最低可执行合同。"""

    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("生成用例合同校验失败: " + "; ".join(self.issues))


# v3.5.10（docs/32 B-3 / F3）：安全负向断言方向写反的确定性检测。
# 形态：type 为正向算子（contains/equals/custom），message 却在说"过滤/移除/
# 拒绝"（负向），expected_value 还放着攻击载荷本体 → 执行即假阴性（断言
# "响应包含 <script>"，或 custom 注释与载荷方向矛盾）。
# 双条件缺一不判（防误伤）：负向词 message ∧ 载荷模式 expected_value。
_NEGATIVE_ASSERTION_MARKERS = ("过滤", "移除", "剔除", "不保存", "拒绝", "拦截")
# 载荷模式（子串、小写比较）。定标来源：DB 全量实测 32+20 条同型残余
# （XSS 事件处理器族 / HTML 标签 / data URI / SQL 布尔注入）。
# 不含提示文本（"非法输入"/"拒绝上传"等）——那是正确正向断言的 expected。
_PAYLOAD_PATTERNS = (
    "<script", "javascript:", "drop table", "onerror",
    "<iframe", "<style", "<img", "<!--",
    "onclick", "onload", "onmouseover", "onfocus", "onblur",
    "<noscript", "<template", "data:text/html", "or 1=1",
)
# 裸 script 用词边界匹配：子串会误伤 description/manuscript 等 innocent 词。
_PAYLOAD_WORD_RE = re.compile(r"\bscript\b")
# 假阳性白名单：这些别名归一后是 custom 但语义是数值/存在性比较，
# 与载荷方向无关，不参与方向判定。
_NON_DIRECTIONAL_CUSTOM_KEYS = ("greater_or_equal", "less_or_equal")


def reversed_security_assertion_issues(assertion: Any, path: str) -> list[str]:
    """检测"安全负向断言方向写反"（纯函数，供校验与存量修复共用同一判定）。

    判定（docs/32 B-3 双条件，缺一不判）：
    - message 含负向词（过滤/移除/剔除/不保存/拒绝/拦截）之一；
    - type 是可翻转的正向算子 且 expected_value 命中攻击载荷模式。

    type 口径（⚠ 定稿偏差，实证优先，待确认）：docs/32 定稿为
    type ∈ {contains, equals}，但 DB 全量定标 32 条反向断言全部是
    type:"custom"（contains/equals 为 0，docs/31 F3 样例 3/4 亦为 custom）
    → 按实证纳入 custom。not_* 负向算子是修复后/原生正确形态，一律豁免
    （防误伤）；greater_or_equal/less_or_equal 归一为 custom 但语义是
    数值比较，排除。

    合法负向断言（type=not_contains/not_in，载荷放 expected_value 作
    "不得出现"对照）不命中；合法正向断言（如"响应包含
    Content-Security-Policy 头"）无载荷模式，不命中。
    """
    if not isinstance(assertion, dict):
        return []
    message = assertion.get("message")
    if not isinstance(message, str):
        return []
    if not any(word in message for word in _NEGATIVE_ASSERTION_MARKERS):
        return []
    key = assertion.get("type")
    key = key.strip().lower() if isinstance(key, str) else ""
    if key.startswith("not_"):
        return []
    if key in _NON_DIRECTIONAL_CUSTOM_KEYS:
        return []
    if ASSERTION_TYPE_MAP.get(key) not in ("contains", "equals", "custom"):
        return []
    expected = assertion.get("expected_value")
    expected_text = str(expected).lower() if expected is not None else ""
    if not any(pat in expected_text for pat in _PAYLOAD_PATTERNS) \
            and not _PAYLOAD_WORD_RE.search(expected_text):
        return []
    return [
        f"{path}.type 安全负向断言方向写反（message 表述过滤/拒绝却用 "
        f"{key} 正向断言载荷本体）：应改用 not_contains/not_in，"
        f"expected_value 保留载荷本体作为「不得出现」的对照"
    ]


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _assertion_issues(assertion: Any, path: str) -> list[str]:
    # 历史人工用例/审核入口存在文本断言，保留其明确的自然语言语义。
    if isinstance(assertion, str):
        return [] if assertion.strip() else [f"{path} 文本断言不能为空"]
    if not isinstance(assertion, dict):
        return [f"{path} 必须是断言对象或非空字符串"]

    issues = []
    raw_type = assertion.get("type")
    key = raw_type.strip().lower() if isinstance(raw_type, str) else ""
    assertion_type = ASSERTION_TYPE_MAP.get(key)
    if assertion_type is None:
        issues.append(f"{path}.type 缺失或不支持")
    for field in ("target", "message"):
        if field in assertion and not isinstance(assertion[field], str):
            issues.append(f"{path}.{field} 必须是字符串")

    if assertion_type == "custom" and key == "custom":
        if not _nonempty_text(assertion.get("message")):
            issues.append(f"{path}.message 自定义断言必须说明校验条件")
    elif assertion_type is not None:
        if not _nonempty_text(assertion.get("target")):
            issues.append(f"{path}.target 必须是非空字符串")
        if key not in ("not_null", "exists", "not_exists") and "expected_value" not in assertion:
            issues.append(f"{path}.expected_value 缺失")
    # v3.5.10（docs/32 B-3）：安全负向断言方向写反 → 内容错误，
    # 经 invalid_case_content → partial_block 走剔坏留好链路。
    issues.extend(reversed_security_assertion_issues(assertion, path))
    return issues


def _validation_issues(
    cases: Any,
    expected_requirement_id: str | None,
    *,
    raw: bool,
) -> list[str]:
    if not isinstance(cases, list) or not cases:
        return ["cases 必须是非空用例列表"]

    issues: list[str] = []
    seen_ids: set[str] = set()
    for index, case in enumerate(cases):
        path = f"cases[{index}]"
        if not isinstance(case, dict):
            issues.append(f"{path} 必须是对象")
            continue

        case_id = case.get("id")
        if _nonempty_text(case_id):
            normalized_id = case_id.strip()
            if normalized_id in seen_ids:
                issues.append(f"{path}.id 重复: {normalized_id}")
            seen_ids.add(normalized_id)
        elif not raw or (case_id is not None and not isinstance(case_id, str)):
            issues.append(f"{path}.id 必须是非空字符串")

        if not _nonempty_text(case.get("title")):
            issues.append(f"{path}.title 必须是非空字符串")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            issues.append(f"{path}.steps 必须是非空步骤列表")
        else:
            for step_index, step in enumerate(steps):
                step_path = f"{path}.steps[{step_index}]"
                if not isinstance(step, dict):
                    issues.append(f"{step_path} 必须是步骤对象")
                    continue
                for field in ("action", "expected"):
                    if not _nonempty_text(step.get(field)):
                        issues.append(f"{step_path}.{field} 必须是非空字符串")

        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            issues.append(f"{path}.assertions 必须是非空断言列表")
        else:
            for assertion_index, assertion in enumerate(assertions):
                issues.extend(_assertion_issues(assertion, f"{path}.assertions[{assertion_index}]"))

        # 原始生成允许省略追溯字段，由服务注入；显式错值不能被覆盖掩盖。
        if raw and "related_requirement" not in case:
            continue
        related_requirement = case.get("related_requirement")
        if not _nonempty_text(related_requirement):
            issues.append(f"{path}.related_requirement 必须是非空字符串")
        elif expected_requirement_id and related_requirement != expected_requirement_id:
            issues.append(f"{path}.related_requirement 与当前需求不一致")
    return issues


def validation_issues(cases: Any, expected_requirement_id: str | None = None) -> list[str]:
    """返回问题列表；不修改输入，不因 JSON 字段错型抛异常。

    不根据 source/reused 等用例自声明豁免关联需求，可信复用由调用方处理。
    """
    return _validation_issues(cases, expected_requirement_id, raw=False)


def validate_generated_cases(cases: Any, expected_requirement_id: str | None = None) -> None:
    """最终用例集校验失败时抛出可由编排层标记失败的专用异常。"""
    issues = validation_issues(cases, expected_requirement_id)
    if issues:
        raise GeneratedCaseValidationError(issues)


def validate_raw_generated_cases(cases: Any, expected_requirement_id: str | None = None) -> None:
    """规范化前验证语义；只允许服务补生成 ID 和缺省需求关联。"""
    issues = _validation_issues(cases, expected_requirement_id, raw=True)
    if issues:
        raise GeneratedCaseValidationError(issues)
