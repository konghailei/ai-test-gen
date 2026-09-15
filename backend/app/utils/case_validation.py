"""生成、审核与入库共用的用例语义合同，不修补缺失的测试内容。"""

from __future__ import annotations

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
    "not_contain": "custom", "not_exists": "custom",
    # UTCF 没有非严格比较枚举，保留操作符含义，不能改写为严格大于/小于。
    "greater_or_equal": "custom", "less_or_equal": "custom",
}


class GeneratedCaseValidationError(ValueError):
    """模型返回的用例不满足最低可执行合同。"""

    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("生成用例合同校验失败: " + "; ".join(self.issues))


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
