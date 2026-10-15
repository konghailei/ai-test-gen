"""Markdown 导出器。

将 UTCF 测试用例列表转换为 Markdown 文档，用于：
- 测试人员在 GitHub/GitLab PR 中 Review
- Git diff 友好，清晰展示用例变更
- 文档归档与线下评审

设计依据：docs/04-output-format.md §9.2（UTCF → Markdown 转换规则）

输出结构（对应 §9.2.2 批次用例文档）：
    # 测试用例文档
    ## 覆盖率概览（按 type 统计的表格）
    ## {类型}测试用例（按 type 分组）
        ### {case_id}: {title}
            优先级/标签/追溯/置信度
            前置条件
            测试步骤表格
            断言
            清理操作
    ## 追溯矩阵（case_id → related_requirement）
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.case_type_taxonomy import (
    GROUP_LABEL,
    case_type_composite_label,
    case_type_group,
    group_members,
    group_sort_key,
)
from app.utils.logger import logger


# ============================================================================
# 类型中文名映射已迁移至 app.core.case_type_taxonomy（v3.5.11，单一权威）
# ============================================================================


# ============================================================================
# 主导出函数
# ============================================================================

def _cell(value) -> str:
    """把任意文本规整为可安全放进 Markdown 表格单元格的单行文本。

    v3.4.1 修复 L-1：换行（\\r\\n / \\n / 单独 \\r）会截断表格行，竖线会多分出一列，
    两者都会破坏表格结构，这里统一先单行化再转义。
    """
    text = str(value if value is not None else "")
    for ch in ("\r\n", "\n", "\r"):
        text = text.replace(ch, " ")
    return text.replace("|", "\\|")


def export_markdown(cases: list[dict]) -> str:
    """将用例列表导出为 Markdown 文档。

    Args:
        cases: 用例 JSON 列表，每项为单条 UTCF TestCase dict

    Returns:
        str: Markdown 格式的测试用例文档

    Note:
        - 字段缺失时使用默认值，不抛异常（保证导出鲁棒性）
        - 用例按 type 分组输出，组内保持原顺序
        - 步骤表格自动补全缺失的 step 序号（用索引+1）
    """
    if not cases:
        return "# 测试用例文档\n\n> 无可导出的用例。\n"

    lines: list[str] = []

    # ========================================================================
    # 文档头
    # ========================================================================
    lines.append("# 测试用例文档")
    lines.append("")
    lines.append(f"**用例数量**: {len(cases)}  ")
    # v3.4.1 修复 L-4：datetime.utcnow() 自 Python 3.12 起已废弃，且返回 naive datetime
    lines.append(f"**导出时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC  ")
    # 从首个用例提取需求 ID（若所有用例关联同一需求）
    req_ids = {c.get("related_requirement") for c in cases if c.get("related_requirement")}
    if len(req_ids) == 1:
        lines.append(f"**关联需求**: {next(iter(req_ids))}  ")
    elif len(req_ids) > 1:
        lines.append(f"**关联需求**: {', '.join(sorted(req_ids))}  ")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ========================================================================
    # 覆盖率概览（按 type 统计）
    # ========================================================================
    lines.append("## 覆盖率概览")
    lines.append("")
    lines.append("| 测试类型 | 数量 |")
    lines.append("|------|------|")
    # v3.5.11：按 taxonomy 分组计数（boundary/exception/exploratory 归功能组）
    type_counts: dict[str, int] = {}
    for case in cases:
        grp = case_type_group(case.get("type", ""))
        type_counts[grp] = type_counts.get(grp, 0) + 1
    for grp in sorted(type_counts, key=group_sort_key):
        lines.append(f"| {GROUP_LABEL.get(grp, grp)} | {type_counts[grp]} |")
    lines.append(f"| **总计** | **{len(cases)}** |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ========================================================================
    # 按测试类型分组输出用例（v3.5.11：按 taxonomy 分组，组内按细分排序）
    # ========================================================================
    cases_by_group: dict[str, list[dict]] = {}
    for case in cases:
        grp = case_type_group(case.get("type", ""))
        cases_by_group.setdefault(grp, []).append(case)

    for grp in sorted(cases_by_group, key=group_sort_key):
        members = group_members(grp)

        def _detail_key(c: dict, _members: tuple[str, ...] = members) -> tuple[int, str]:
            t = (c.get("type") or "").strip().lower()
            return (_members.index(t) if t in _members else len(_members), c.get("id") or "")

        group_cases = sorted(cases_by_group[grp], key=_detail_key)
        type_label = GROUP_LABEL.get(grp, grp)
        lines.append(f"## {type_label}用例")
        lines.append("")

        for case in group_cases:
            _append_case_markdown(lines, case)
            lines.append("")

    # ========================================================================
    # 追溯矩阵
    # ========================================================================
    lines.append("## 追溯矩阵")
    lines.append("")
    lines.append("| 用例 ID | 关联需求 | 置信度 |")
    lines.append("|---------|---------|--------|")
    for case in cases:
        case_id = case.get("id", "N/A")
        req = case.get("related_requirement") or "—"
        conf = case.get("confidence")
        conf_str = f"{int(conf * 100)}%" if isinstance(conf, (int, float)) else "—"
        lines.append(f"| {case_id} | {req} | {conf_str} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**AI 生成备注**: 此文档由 AI测试用例生成系统 生成，如有问题请在 Review 中标注。")
    lines.append("")

    logger.debug("[Exporter/Markdown] 生成完成: cases={}, types={}", len(cases), list(type_counts.keys()))
    return "\n".join(lines)


# ============================================================================
# 单个用例的 Markdown 生成
# ============================================================================

def _append_case_markdown(lines: list[str], case: dict) -> None:
    """将单个用例追加为 Markdown 章节。

    Args:
        lines: 行列表（in-place 追加）
        case: 单条用例 dict
    """
    case_id = case.get("id", "UNKNOWN")
    title = case.get("title", "（无标题）")
    priority = case.get("priority", "N/A")
    typ = case.get("type", "unknown")
    tags = case.get("tags") or []
    req = case.get("related_requirement") or "N/A"
    conf = case.get("confidence")
    source_pattern = case.get("source_pattern") or ""

    # 标题行
    lines.append(f"### {case_id}: {title}")
    lines.append("")

    # 元信息行
    tags_str = " ".join(f"`{t}`" for t in tags) if tags else "—"
    conf_str = f"{int(conf * 100)}%" if isinstance(conf, (int, float)) else "N/A"
    lines.append(f"**类型**: {case_type_composite_label(typ)} | **优先级**: {priority} | **标签**: {tags_str}  ")
    lines.append(f"**追溯**: {req} | **置信度**: {conf_str}" + (f" | **参考模式**: {source_pattern}" if source_pattern else ""))
    lines.append("")

    # 前置条件
    preconditions = case.get("preconditions") or []
    if preconditions:
        lines.append("**前置条件:**")
        lines.append("")
        for pc in preconditions:
            lines.append(f"- [ ] {pc}")
        lines.append("")

    # 测试步骤表格
    steps = case.get("steps") or []
    if steps:
        lines.append("**测试步骤:**")
        lines.append("")
        lines.append("| 步骤 | 操作 | 预期结果 |")
        lines.append("|------|------|---------|")
        for idx, step in enumerate(steps, start=1):
            step_no = step.get("step", idx)
            # v3.4.1 修复 L-1：Markdown 表格单元格内不能有换行，否则表格被撑裂；
            # 先单行化再转义竖线，避免破坏表格结构
            action = _cell(step.get("action", ""))
            expected = _cell(step.get("expected", ""))
            lines.append(f"| {step_no} | {action} | {expected} |")
        lines.append("")

    # 整体预期结果（可选）
    overall_expected = case.get("expected")
    if overall_expected:
        lines.append(f"**整体预期**: {overall_expected}")
        lines.append("")

    # 测试数据（可选）
    test_data = case.get("test_data") or {}
    if test_data:
        lines.append("**测试数据:**")
        lines.append("")
        lines.append("```json")
        import json
        lines.append(json.dumps(test_data, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    # 断言（可选）
    assertions = case.get("assertions") or []
    if assertions:
        lines.append("**断言:**")
        lines.append("")
        for assertion in assertions:
            a_type = assertion.get("type", "")
            target = assertion.get("target", "")
            expected_val = assertion.get("expected_value", "")
            message = assertion.get("message", "")
            line = f"- [ ] `{target}` {a_type}" + (f" `{expected_val}`" if expected_val != "" else "")
            if message:
                line += f" — {message}"
            lines.append(line)
        lines.append("")

    # 执行配置（可选）
    execution = case.get("execution") or {}
    if execution:
        timeout = execution.get("timeout_seconds", "—")
        retry = execution.get("retry_count", "—")
        depends = execution.get("depends_on") or []
        depends_str = ", ".join(depends) if depends else "无"
        lines.append("**执行配置:**")
        lines.append("")
        lines.append(f"- **超时**: {timeout} 秒")
        lines.append(f"- **重试次数**: {retry}")
        lines.append(f"- **依赖用例**: {depends_str}")
        lines.append("")

    # 清理操作（可选）
    cleanup = case.get("cleanup") or []
    if cleanup:
        lines.append("**清理操作:**")
        lines.append("")
        for op in cleanup:
            lines.append(f"- [ ] {op}")
        lines.append("")

    lines.append("---")
