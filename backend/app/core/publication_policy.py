"""Publication is a server-side decision, never a synonym for generation finished.

Policy v1 keeps the existing 80% point / 100% P0-P1 coverage limits. It does
not change numeric quality scores to disguise missing evidence. Missing
context fails closed to human review; malformed or empty content is blocked.

v3.5.2（docs/21 方案B"剔坏留好"）：内容级缺陷从"整批连坐"改为"逐条剔除"。
- 批次级致命问题（整批为空）仍 blocked——没有可救的产物。
- 内容级问题（坏条/审核 blocker）只指向个别用例 → partial_block：
  坏条不落库，剩余条按置信度正常分级入库；质量信号（coverage 等）
  属于批次级 soft signal，只影响 human_review，不拦截好条入库。
- 这与 Meta TestGen-LLM 的过滤哲学一致：过滤器淘汰坏条，存活者出货。
"""
from __future__ import annotations

import math
import re
from typing import Any

POLICY_VERSION = "quality-v2"

# validation_issues 返回 "cases[N].field ..." 形式的路径，可定位到条。
_CASE_PATH_RE = re.compile(r"^cases\[(\d+)\]")


def _case_level_issue_count(content_errors: list[str] | None) -> int:
    """统计能定位到具体用例的内容级问题条数（cases[N] 前缀）。"""
    return sum(1 for e in (content_errors or []) if _CASE_PATH_RE.match(str(e)))


def _ratio(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


def decide_publication(
    report: dict | None,
    cases: list[dict],
    *,
    gate_result: dict | None = None,
    forced_pass: bool = False,
    degraded_flags: list[str] | None = None,
    failed_subtasks: list[dict] | None = None,
    content_errors: list[str] | None = None,
) -> dict:
    """Require positive evidence for automatic publication (pure function)."""
    reasons: list[str] = []
    case_level_reasons: list[str] = []
    report = report if isinstance(report, dict) else {}
    gate = gate_result if isinstance(gate_result, dict) else {}
    if not cases:
        reasons.append("empty_generation")
    # 报告里已记录的合同问题同样成立（ReviewAgent 只记录不上抛，这里必须认）。
    # v3.5.2: 内容级问题计入 case_level_reasons（可定位到条 → partial_block 语义），
    # 不再直接把整批判死。
    content_issue_count = 0
    if content_errors or report.get("content_errors"):
        content_issue_count = _case_level_issue_count(content_errors) or len(
            content_errors or report.get("content_errors") or []
        )
        case_level_reasons.append("invalid_case_content")
    blocked = bool(reasons)

    if gate.get("passed") is not True or gate.get("blockers"):
        reasons.append("quality_gate_not_passed")
    if forced_pass or gate.get("forced_pass"):
        reasons.append("quality_gate_forced")
    if report.get("review_degraded"):
        reasons.append("review_llm_failed")
    if report.get("review_status") != "complete":
        reasons.append("review_not_verified")
    if report.get("total_cases") != len(cases):
        reasons.append("review_case_count_mismatch")
    issues = report.get("issues")
    if not isinstance(issues, list):
        reasons.append("review_issues_unavailable")
    elif any(not isinstance(i, dict) or i.get("severity") == "blocker" for i in issues):
        # v3.5.2: 审核 blocker 是逐条 issue（带 case_id），归入 case_level_reasons，
        # 坏条剔除后剩余条照常入库；不再整批连坐。
        case_level_reasons.append("review_blockers")
    if report.get("should_supplement") or report.get("supplement_exhausted"):
        reasons.append("review_gaps_unresolved")

    coverage = report.get("point_coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    rate = _ratio(coverage.get("coverage_rate"))
    if coverage.get("coverage_status") != "mapped" or coverage.get("mapping_based") is not True:
        reasons.append("coverage_unknown")
    elif rate is None or rate < 0.8:
        reasons.append("coverage_insufficient")
    high_total = coverage.get("high_priority_total", 0)
    if isinstance(high_total, (int, float)) and high_total > 0:
        hp_rate = _ratio(coverage.get("high_priority_rate"))
        if hp_rate is None or hp_rate < 1:
            reasons.append("high_priority_coverage_incomplete")
    if coverage.get("invalid_point_ids"):
        reasons.append("invalid_point_references")
    if failed_subtasks:
        reasons.append("generation_subtasks_failed")
    for flag in [*(degraded_flags or []), *(report.get("degraded_flags") or [])]:
        if isinstance(flag, str) and flag:
            reasons.append(flag)
    reasons = list(dict.fromkeys(reasons))
    case_level_reasons = list(dict.fromkeys(case_level_reasons))
    # v3.5.2 决策矩阵：
    # - blocked：批次级致命（empty_generation）——没有可救的产物
    # - partial_block：批次本身存活 + 存在可定位到条的内容级缺陷——
    #   坏条剔除，剩余按置信度正常分级入库；批次级质量信号照常透出
    # - human_review / auto_approve：与 v1 相同
    if blocked:
        decision = "blocked"
    elif case_level_reasons:
        decision = "partial_block"
    elif reasons:
        decision = "human_review"
    else:
        decision = "auto_approve"
    return {
        "decision": decision,
        "reasons": reasons,
        "case_level_reasons": case_level_reasons,
        "case_level_issue_count": content_issue_count,
        "policy_version": POLICY_VERSION,
    }


def quality_outcome(event: dict) -> dict:
    """Fields shared by SSE and persisted task results. Unknown is not success."""
    report = event.get("review_report") or {}
    publication = event.get("publication") or report.get("publication") or {
        "decision": "human_review", "reasons": ["publication_not_verified"],
        "policy_version": POLICY_VERSION,
    }
    flags = list(dict.fromkeys(event.get("degraded_flags") or []))
    flowback = event.get("kb_flowback") or {}
    if (flowback.get("error") or flowback.get("errors") or flowback.get("vector_write_failed")):
        flags = list(dict.fromkeys([*flags, "kb_publication_incomplete"]))
        publication = {**publication, "decision": "human_review", "reasons": list(dict.fromkeys([
            *(publication.get("reasons") or []), "kb_publication_incomplete",
        ]))}
    manual = (publication.get("decision") != "auto_approve" or bool(flags)
              or bool(event.get("manual_action_required")))
    # v3.5.2: 只有批次级 blocked 才 failed；partial_block（剔坏留好）的
    # 好条已入库，任务是 awaiting_human_review 而非 failed。
    failed = publication.get("decision") == "blocked" or not event.get("cases")
    return {
        "completion_status": "failed" if failed else "awaiting_human_review" if manual else "completed",
        "publication": publication,
        "degraded_flags": flags,
        "failed_subtasks": event.get("failed_subtasks") or [],
        "manual_action_required": manual or failed,
        "review_degraded": bool(report.get("review_degraded")),
        "gate_forced": bool((event.get("gate_result") or {}).get("forced_pass")),
    }
