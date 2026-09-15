"""评估运行与 prompt 激活门槛的离线用例（docs/15 §4.5 P0-5）。

不连 DB、不调 LLM：只验证"凭什么放行"的判定逻辑与评分口径。
"""
import pytest

from app.core.evaluation import (
    DEFAULT_PASS_THRESHOLD,
    PROMPT_EVAL_SCHEMA_VERSION,
    dataset_hash,
    score_dataset,
    score_sample,
    sha256_text,
    validate_prompt_activation,
)

CANDIDATE = "你是资深测试设计专家。输出用例时必须包含 assertions / steps / related_requirement。"


def _run(**over):
    base = {
        "id": "EV-1", "kind": "prompt_candidate", "target": "gen_agent.SYSTEM_PROMPT",
        "status": "passed", "dataset_hash": dataset_hash([{"id": "s1"}]),
        "prompt_hash": sha256_text(CANDIDATE), "schema_version": PROMPT_EVAL_SCHEMA_VERSION,
        "metrics": {"baseline_evaluated": True}, "score": 1.0,
    }
    base.update(over)
    return base


def _activate(run):
    return validate_prompt_activation(
        run, target="gen_agent.SYSTEM_PROMPT", candidate_prompt=CANDIDATE,
    )


def test_valid_passed_run_authorizes_activation():
    ok, reason = _activate(_run())
    assert ok, reason


def test_missing_run_is_rejected():
    ok, reason = _activate(None)
    assert not ok
    assert "评估运行" in reason and "confirmed_eval" in reason


@pytest.mark.parametrize("over,fragment", [
    ({"status": "failed"}, "只有 passed"),
    ({"status": "error"}, "只有 passed"),
    ({"target": "gen_agent.OTHER"}, "与本次激活目标"),
    ({"dataset_hash": ""}, "没有数据集 hash"),
    ({"schema_version": "prompt-eval-v0"}, "口径版本"),
    ({"prompt_hash": sha256_text("被改过的候选文本")}, "候选文本已变更"),
])
def test_each_gate_cannot_be_bypassed(over, fragment):
    ok, reason = _activate(_run(**over))
    assert not ok
    assert fragment in reason


def test_prompt_hash_is_exact_not_normalized():
    # 仅改动空白字符也必须重新评估（否则"评估 A、上线 B"）
    ok, _ = _activate(_run(prompt_hash=sha256_text(CANDIDATE + "\n")))
    assert not ok


@pytest.mark.parametrize("metrics,fragment", [
    ({}, "没有基线对比记录"),
    ({"baseline_evaluated": False}, "没有基线对比记录"),
])
def test_run_without_baseline_evidence_is_rejected(metrics, fragment):
    ok, reason = _activate(_run(metrics=metrics))
    assert not ok and fragment in reason


def test_baseline_unavailable_with_reason_is_accepted():
    # 新提示词家族没有"当前线上版本"可比时，脚本会显式记录原因，允许放行
    ok, reason = _activate(_run(metrics={
        "baseline_unavailable": True,
        "baseline_unavailable_reason": "取不到当前线上提示词文本",
    }))
    assert ok, reason


def test_score_sample_needs_keypoints_present():
    passed, ratio, missed = score_sample("输出包含 assertions 与 steps 字段", ["assertions", "steps"])
    assert passed and ratio == 1.0 and missed == []

    passed, ratio, missed = score_sample("输出只有 steps", ["assertions", "steps"])
    assert not passed and ratio == 0.5 and missed == ["assertions"]


def test_score_sample_without_expectations_never_passes():
    # 没有期望要点 = 无法判定，不能白送分
    passed, ratio, _ = score_sample("任意输出", [])
    assert not passed and ratio == 0.0


def test_score_sample_ignores_whitespace_and_case():
    passed, _, _ = score_sample("related_Requirement  \n 已给出", ["related_requirement"])
    assert passed


def test_score_dataset_aggregates():
    metrics = score_dataset([
        {"passed": True, "hit_ratio": 1.0},
        {"passed": False, "hit_ratio": 0.5},
        {"passed": True, "hit_ratio": 0.75},
        {"passed": True, "hit_ratio": 1.0},
    ])
    assert metrics["sample_count"] == 4
    assert metrics["passed_count"] == 3
    assert metrics["score"] == 0.75
    assert metrics["hit_ratio_mean"] == pytest.approx(0.8125, abs=0.001)


def test_dataset_hash_changes_with_content():
    a = dataset_hash([{"id": "s1", "must_cover": ["assertions"]}])
    b = dataset_hash([{"id": "s1", "must_cover": ["assertions", "steps"]}])
    assert a != b
    assert a == dataset_hash([{"must_cover": ["assertions"], "id": "s1"}])


def test_default_threshold_is_the_documented_one():
    assert DEFAULT_PASS_THRESHOLD == 0.8
