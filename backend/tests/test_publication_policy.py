"""Publication policy regression tests: no LLM, database or network required."""
from copy import deepcopy

import pytest

from app.core.publication_policy import decide_publication, quality_outcome


def green_report():
    return {
        "total_cases": 1, "issues": [], "review_status": "complete",
        "review_degraded": False, "should_supplement": False,
        "point_coverage": {
            "coverage_status": "mapped", "mapping_based": True,
            "coverage_rate": 1.0, "high_priority_total": 1,
            "high_priority_rate": 1.0,
        },
    }


def test_positive_evidence_allows_automatic_publication():
    result = decide_publication(green_report(), [{"id": "TC-1"}], gate_result={"passed": True})
    assert result["decision"] == "auto_approve"
    assert result["reasons"] == []


@pytest.mark.parametrize("changes,reason", [
    ({"review_degraded": True}, "review_llm_failed"),
    ({"review_status": "degraded"}, "review_not_verified"),
    ({"review_status": None}, "review_not_verified"),
    ({"total_cases": 9}, "review_case_count_mismatch"),
    ({"issues": None}, "review_issues_unavailable"),
    ({"issues": [{"severity": "blocker"}]}, "review_blockers"),
    ({"should_supplement": True}, "review_gaps_unresolved"),
    ({"supplement_exhausted": True}, "review_gaps_unresolved"),
    ({"point_coverage": {}}, "coverage_unknown"),
])
def test_report_failure_never_auto_approves(changes, reason):
    report = green_report()
    report.update(changes)
    result = decide_publication(report, [{"id": "TC-1"}], gate_result={"passed": True})
    assert result["decision"] == "human_review"
    assert reason in result["reasons"]


@pytest.mark.parametrize("kwargs,reason", [
    ({}, "quality_gate_not_passed"),
    ({"gate_result": {"passed": False}}, "quality_gate_not_passed"),
    ({"gate_result": {"passed": True, "forced_pass": True}}, "quality_gate_forced"),
    ({"gate_result": {"passed": True}, "forced_pass": True}, "quality_gate_forced"),
    ({"gate_result": {"passed": True}, "degraded_flags": ["integration_generate_failed"]}, "integration_generate_failed"),
    ({"gate_result": {"passed": True}, "failed_subtasks": [{"key": "FP-A"}]}, "generation_subtasks_failed"),
])
def test_pipeline_failure_requires_human(kwargs, reason):
    result = decide_publication(green_report(), [{"id": "TC-1"}], **kwargs)
    assert result["decision"] == "human_review"
    assert reason in result["reasons"]


@pytest.mark.parametrize("coverage", [
    {"coverage_status": "unknown", "coverage_rate": 1.0},
    {"coverage_rate": float("nan")}, {"coverage_rate": float("inf")},
    {"coverage_rate": 1.1}, {"coverage_rate": True}, {"coverage_rate": 0.7999},
    {"high_priority_rate": 0.99}, {"invalid_point_ids": ["alien.rule.1"]},
])
def test_missing_or_invalid_mapping_cannot_authorize(coverage):
    report = green_report()
    report["point_coverage"].update(coverage)
    result = decide_publication(report, [{"id": "TC-1"}], gate_result={"passed": True})
    assert result["decision"] != "auto_approve"


def test_empty_and_invalid_content_are_blocked():
    assert decide_publication(green_report(), [], gate_result={"passed": True})["decision"] == "blocked"
    assert decide_publication(green_report(), [{}], content_errors=["empty steps"])["decision"] == "blocked"


def test_explicit_success_vs_legacy_unknown():
    event = {"cases": [{"id": "TC-1"}], "review_report": green_report()}
    original = deepcopy(event)
    assert quality_outcome(event)["completion_status"] == "awaiting_human_review"
    assert event == original
    event["publication"] = {"decision": "auto_approve", "reasons": [], "policy_version": "quality-v1"}
    assert quality_outcome(event)["completion_status"] == "completed"
    event["kb_flowback"] = {"vector_write_failed": 1}
    outcome = quality_outcome(event)
    assert outcome["completion_status"] == "awaiting_human_review"
    assert "kb_publication_incomplete" in outcome["degraded_flags"]


def test_empty_done_is_failed_not_complete():
    assert quality_outcome({"cases": []})["completion_status"] == "failed"
