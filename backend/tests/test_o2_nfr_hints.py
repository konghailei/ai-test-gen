"""O-2 NFR 阈值缺口进 supplement_hints 优先位（docs/32 O-2）。

背景：本轮 5 项零命中阈值（限流 60/min、禁用 600s、召回 20 条、
用例上下限 40、校验 512 字节）全是 NFR 阈值类要点——泛化补生成常只补
happy path，阈值边界/超限验证没人管，且 hints 里没有定向提示。

口径：非功能点 且 描述带数值阈值。命中者拆出置 hints 首位
（case_type=coverage_gap + priority=nfr_threshold），其余留通用 coverage_gap；
无 NFR 时输出与旧行为字节一致（回归锁）。

全部确定性断言（不调 LLM、不连 DB）。
"""
from __future__ import annotations

from app.agents.review_agent import ReviewAgent, _is_nfr_threshold_point

_ALL_COVERED = {"boundary": 1, "exception": 1, "security": 1, "performance": 1}


def _pt(pid: str, ptype: str, desc: str, fp_id: str = "FP-1") -> dict:
    return {"id": pid, "type": ptype, "priority": "P1", "desc": desc, "fp_id": fp_id}


def test_nfr_threshold_point_detection():
    """判定口径：non_functional ∧ 描述带数值阈值。"""
    # docs/32 点名的 5 项形态全命中
    assert _is_nfr_threshold_point(_pt("non_functional.1", "non_functional", "限流 60/min"))
    assert _is_nfr_threshold_point(_pt("non_functional.2", "non_functional", "禁用 600s"))
    assert _is_nfr_threshold_point(_pt("non_functional.3", "non_functional", "召回 20 条"))
    assert _is_nfr_threshold_point(_pt("non_functional.4", "non_functional", "用例上下限 40"))
    assert _is_nfr_threshold_point(_pt("non_functional.5", "non_functional", "校验 512 字节"))
    # 纯文字 NFR 无阈值可验 → 不算
    assert not _is_nfr_threshold_point(_pt("non_functional.6", "non_functional", "传输加密"))
    # 非 NFR 类型带数字 → 不算（阈值归属是 NFR 的事）
    assert not _is_nfr_threshold_point(_pt("rule.1", "rule", "限流 60/min"))
    # 非 dict 容错
    assert not _is_nfr_threshold_point("not-a-dict")


def test_nfr_hints_priority_position_and_partition():
    """NFR 阈值缺口置 hints 首位；通用缺口只列其余要点（不重复）。"""
    agent = ReviewAgent()
    nfr = _pt("non_functional.1", "non_functional", "限流 60/min")
    nfr2 = _pt("non_functional.2", "non_functional", "校验 512 字节")
    rest = _pt("happy_path.1", "happy_path", "正常登录")
    hints = agent._gen_supplement_hints(
        _ALL_COVERED, {},
        {"uncovered": [rest, nfr, nfr2]},
    )
    assert hints, "应有 hints"
    first = hints[0]
    assert first.get("priority") == "nfr_threshold", first
    assert first.get("case_type") == "coverage_gap", first
    assert "NFR 阈值要点未覆盖" in first.get("reason", ""), first
    assert [p["id"] for p in first["uncovered_points"]] == [
        "non_functional.1", "non_functional.2"], first
    # 通用 coverage_gap 只含其余要点，NFR 不重复出现
    general = [h for h in hints if h.get("case_type") == "coverage_gap"
               and h.get("priority") != "nfr_threshold"]
    assert len(general) == 1, hints
    assert [p["id"] for p in general[0]["uncovered_points"]] == ["happy_path.1"], general
    assert general[0]["reason"] == "1 个 SRR 要点未覆盖，需定向补生成", general


def test_general_hint_unchanged_without_nfr():
    """无 NFR 缺口 → 输出与旧行为一致（回归锁：单条通用 coverage_gap）。"""
    agent = ReviewAgent()
    pts = [_pt("happy_path.1", "happy_path", "正常登录"),
           _pt("non_functional.1", "non_functional", "传输加密")]
    hints = agent._gen_supplement_hints(_ALL_COVERED, {}, {"uncovered": pts})
    gap = [h for h in hints if h.get("case_type") == "coverage_gap"]
    assert len(gap) == 1, hints
    assert gap[0]["reason"] == "2 个 SRR 要点未覆盖，需定向补生成", gap
    assert "priority" not in gap[0], gap
    assert [p["id"] for p in gap[0]["uncovered_points"]] == [
        "happy_path.1", "non_functional.1"], gap


def test_all_nfr_only_emits_priority_hint():
    """全是 NFR 缺口时只发优先位提示，不再发空的通用提示。"""
    agent = ReviewAgent()
    hints = agent._gen_supplement_hints(
        _ALL_COVERED, {},
        {"uncovered": [_pt("non_functional.1", "non_functional", "禁用 600s")]},
    )
    gap = [h for h in hints if h.get("case_type") == "coverage_gap"]
    assert len(gap) == 1, hints
    assert gap[0].get("priority") == "nfr_threshold", gap
    assert hints[0] is gap[0], hints
