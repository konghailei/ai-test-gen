"""Publication is a server-side decision, never a synonym for generation finished.

Policy v1 keeps the existing 80% point / 100% P0-P1 coverage limits. It does
not change numeric quality scores to disguise missing evidence. Missing
context fails closed to human review; malformed or empty content is blocked.
"""
from __future__ import annotations

import math
from typing import Any

POLICY_VERSION = "quality-v1"


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
    report = report if isinstance(report, dict) else {}
    gate = gate_result if isinstance(gate_result, dict) else {}
    if not cases:
        reasons.append("empty_generation")
    # 报告里已记录的合同问题同样成立（ReviewAgent 只记录不上抛，这里必须认）
    if content_errors or report.get("content_errors"):
        reasons.append("invalid_case_content")
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
        reasons.append("review_blockers")
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
    return {
        "decision": "blocked" if blocked else "human_review" if reasons else "auto_approve",
        "reasons": reasons,
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
