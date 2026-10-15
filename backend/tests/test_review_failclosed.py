# -*- coding: utf-8 -*-
"""v3.5.10 批一守卫：分片审核 fail-closed（B-2）+ auto 连坐终版放开（B-1）。

对应 docs/32 验收标准（全部按结构字段断言，不匹配 LLM 自由文本）：
1. 注入 1 个分片 `_run_shard` 异常 → 最终 report 含 `shard_review.failed_shards`、
   `review_shard_partial_failure=True`、`review_status="partial"`；
   orchestrator `degraded_flags` 含 `review_shard_partial_failure`；
2. 失败分片用例落 `reviewed` 而非 `approved`（persist 消费侧，docs/31 F1 回归）；
3. except 分支对称性（MEMORY 教训）：done（completed/failed 两形态）与
   error/except 路径逐分支断言——done 带两键、失败分支不得用空 result_json 覆盖；
4. 三形态叠加：好条 approved / 失败分片条 reviewed / blocker 条不 auto。

unit 层：不连真 PG/Milvus（行为级 fake session，先例 test_provider_disable_retry.py）。
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app")

from app.agents.review_agent import ReviewAgent
from app.core.constants import Confidence

AUTO = Confidence.AUTO_APPROVE_THRESHOLD
DEFAULT_CONF = Confidence.DEFAULT


# ============================================================================
# 夹具与工具
# ============================================================================

SRR_MIN = {
    "requirement": {
        "id": "REQ-2024-001",
        "product_id": "PROD-001",
        "title": "登录功能",
        "modules": ["auth"],
    },
}


def _clean_case(cid: str, fp: str, title: str | None = None, conf: float = 0.85) -> dict:
    """满足可执行合同的干净用例（结构同 GenAgent 真实产物）。"""
    return {
        "id": cid,
        "title": title or f"用例{cid}",
        "type": "functional",
        "priority": "P1",
        "preconditions": ["用户已登录"],
        "steps": [{"step": 1, "action": "执行操作", "expected": "操作成功"}],
        "assertions": [
            {"type": "equals", "target": "operation.result",
             "expected_value": "操作成功", "message": "操作成功断言"},
        ],
        "related_requirement": "REQ-2024-001",
        "related_fp": fp,
        "gen_metadata": {"gen_confidence": 0.85},
        "confidence": conf,
    }


def _report(cases: list[dict], **overrides) -> dict:
    """手工构造审核报告（persist 消费侧用，字段与 _merge_review_results 同形）。"""
    base = {
        "total_cases": len(cases),
        "passed_cases": len(cases),
        "blocked_cases": 0,
        "issues": [],
        "coverage_summary": {},
        "point_coverage": {},
        "avg_confidence": 0.85,
        "should_supplement": False,
        "supplement_hints": [],
        "review_status": "complete",
        "rule_passed_cases": len(cases),
        "content_errors": [],
        "review_degraded": False,
        "review_degraded_reason": "",
    }
    base.update(overrides)
    return base


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def first(self):
        return self._row

    def all(self):
        return [self._row] if self._row is not None else []


class _FakeSession:
    """行为级 fake session：记录 add 的 ORM 对象与 UPDATE 编译参数，不连真 PG。

    unit 层禁连真 PG（function-scoped event loop 下全局引擎连接池绑定旧 loop
    → Event loop is closed），沿用 test_provider_disable_retry.py 先例。
    """

    def __init__(self, select_row=None):
        self.added: list = []
        self.update_params: list[dict] = []
        self.committed = False
        self._select_row = select_row

    async def execute(self, stmt):
        text = str(stmt).lstrip().upper()
        if text.startswith("UPDATE"):
            self.update_params.append(stmt.compile().params)
            return _FakeResult()
        return _FakeResult(self._select_row)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ShardFakeLLM:
    """分片审核假 LLM：指定 FP 的组内审核调用注入异常，其余返回合法空报告。

    组内/跨组判别用 schema_hint（组内带 avg_confidence，跨组只有 issues）——
    跨组 prompt 里同样含各分片 fp_id，不能只看 prompt 文本。
    """

    def __init__(self, fail_fp_ids):
        self.fail_fp_ids = set(fail_fp_ids)
        self.group_calls = 0
        self.cross_calls = 0

    async def call_json(self, *, prompt, system_prompt=None, schema_hint=None,
                        temperature=None, agent_name=None, provider=None, **kwargs):
        if "avg_confidence" in (schema_hint or {}):
            self.group_calls += 1
            if any(f"【本分片功能点】{fid}" in prompt for fid in self.fail_fp_ids):
                raise RuntimeError("injected shard failure")
        else:
            self.cross_calls += 1
        return {"issues": [], "avg_confidence": 0.8}


def _patch_review_seams(monkeypatch, fake_session):
    """patch 掉 review 链路的 DB/提示词外部依赖（judge 供应商读取、prompt 覆盖查询）。"""
    monkeypatch.setattr("app.db.database.async_session", lambda: fake_session)
    monkeypatch.setattr(
        "app.api.settings.get_review_llm_provider", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.core.prompt_manager.get", lambda m, n: "SYSTEM")

    async def _fake_conf(self, srr, cases, skip_search_ids=None):
        for c in cases:
            c["confidence"] = c.get("confidence", 0.85)
        return cases

    monkeypatch.setattr(ReviewAgent, "_calculate_confidences", _fake_conf)


# ============================================================================
# 组 1：_persist_route 纯函数——三形态叠加 + 边界（B-1 × B-2）
# ============================================================================


def test_persist_route_three_form_superposition():
    """docs/32「与 B-1 的交互」：好条 approved / 失败分片条 reviewed / blocker 条不 auto。"""
    route = ReviewAgent._persist_route
    assert route(True, 0.85, False, False) == "approved"   # 好条：高置信已审
    assert route(True, 0.92, False, True) == "reviewed"    # 失败分片条：未审不进 auto（F1）
    assert route(True, 0.85, True, False) == "reviewed"    # blocker 条：不 auto


def test_persist_route_boundaries_and_reject():
    route = ReviewAgent._persist_route
    assert route(True, AUTO, False, False) == "approved"           # 阈值边界含等于
    assert route(True, AUTO - 0.001, False, False) == "reviewed"
    assert route(True, DEFAULT_CONF, False, False) == "reviewed"
    assert route(True, DEFAULT_CONF - 0.001, False, False) == "rejected"
    assert route(True, 0.30, True, False) == "rejected"            # blocker 低置信 → rejected
    # 同因延伸：review_degraded 整批未审 → 低置信也落 reviewed 待人工，不直接拒
    assert route(True, 0.30, False, True) == "reviewed"
    # 参数语义保留：显式不允许 auto 时高置信也不进 auto
    assert route(False, 0.95, False, False) == "reviewed"


# ============================================================================
# 组 2：分片失败标记产出 + 穿透 _merge_review_results（F1 根因回归）
# ============================================================================


@pytest.mark.asyncio
async def test_shard_partial_failure_marks_survive_merge(monkeypatch):
    """注入 1 个分片 _run_shard 异常 → 两键 + review_status="partial" 到达最终 report。"""
    fake = _FakeSession()
    _patch_review_seams(monkeypatch, fake)
    agent = ReviewAgent()
    agent._llm = _ShardFakeLLM({"FP-B"})

    cases = (
        [_clean_case(f"TC-A-{i:02d}", "FP-A", title=f"甲功能点用例{i:02d}") for i in range(13)]
        + [_clean_case(f"TC-B-{i:02d}", "FP-B", title=f"乙功能点用例{i:02d}") for i in range(13)]
    )
    report, _ = await agent.review(SRR_MIN, cases)

    assert report["review_degraded"] is False
    assert report["review_shard_partial_failure"] is True
    assert report["review_status"] == "partial"
    shard = report["shard_review"]
    assert shard["total_shards"] == 2
    assert shard["ok_shards"] == 1
    assert [f["fp_id"] for f in shard["failed_shards"]] == ["FP-B"]
    assert "injected shard failure" in shard["failed_shards"][0]["error"]


@pytest.mark.asyncio
async def test_shard_all_failed_marks_review_degraded(monkeypatch):
    """分片全失败 → review_degraded 终态 + shard_review.failed_shards 非空（fail-closed）。"""
    fake = _FakeSession()
    _patch_review_seams(monkeypatch, fake)
    agent = ReviewAgent()
    agent._llm = _ShardFakeLLM({"FP-A", "FP-B"})

    cases = (
        [_clean_case(f"TC-A-{i:02d}", "FP-A", title=f"甲功能点用例{i:02d}") for i in range(13)]
        + [_clean_case(f"TC-B-{i:02d}", "FP-B", title=f"乙功能点用例{i:02d}") for i in range(13)]
    )
    report, _ = await agent.review(SRR_MIN, cases)

    assert report["review_degraded"] is True
    assert report["review_status"] == "degraded"
    assert report["passed_cases"] == 0
    shard = report["shard_review"]
    assert shard["ok_shards"] == 0 and shard["total_shards"] == 2
    assert sorted(f["fp_id"] for f in shard["failed_shards"]) == ["FP-A", "FP-B"]


def test_merge_review_results_preserves_shard_evidence():
    """F1 根因钉死：合并重建 dict 时不得丢弃 shard_review / review_shard_partial_failure。"""
    agent = ReviewAgent()
    cases = [_clean_case("TC-1", "FP-A")]
    base = {
        "issues": [],
        "avg_confidence": 0.8,
        "shard_review": {
            "total_shards": 2,
            "ok_shards": 1,
            "failed_shards": [{"fp_id": "FP-B", "error": "boom"}],
        },
        "review_shard_partial_failure": True,
    }
    report = agent._merge_review_results(
        cases, [], base, SRR_MIN,
        point_coverage={"total_points": 0, "covered_points": 0},
    )
    assert report["shard_review"] == base["shard_review"]
    assert report["review_shard_partial_failure"] is True
    assert report["review_status"] == "partial"

    degraded = dict(base, review_degraded=True, review_degraded_reason="llm_down")
    report2 = agent._merge_review_results(
        cases, [], degraded, SRR_MIN,
        point_coverage={"total_points": 0, "covered_points": 0},
    )
    assert report2["review_status"] == "degraded"
    assert report2["shard_review"] == base["shard_review"]  # 降级时证据同样保留

    clean = {"issues": [], "avg_confidence": 0.8}
    report3 = agent._merge_review_results(
        cases, [], clean, SRR_MIN,
        point_coverage={"total_points": 0, "covered_points": 0},
    )
    assert report3["review_status"] == "complete"
    assert report3["review_shard_partial_failure"] is False


# ============================================================================
# 组 3：persist_to_kb 消费侧——未审拦截 / 同因延伸 / 剔坏留好 / 低置信
# ============================================================================


@pytest.mark.asyncio
async def test_persist_unreviewed_shard_cases_land_reviewed(monkeypatch):
    """F1 fail-closed 消费侧：失败分片用例即使高置信也落 reviewed，不进 auto。"""
    fake = _FakeSession()
    monkeypatch.setattr("app.db.database.async_session", lambda: fake)
    agent = ReviewAgent()
    good = _clean_case("TC-GOOD", "FP-A", title="好条-已审分片", conf=0.85)
    unrev = _clean_case("TC-UNREV", "FP-B", title="未审分片条", conf=0.92)
    report = _report(
        [good, unrev],
        review_status="partial",
        review_shard_partial_failure=True,
        shard_review={
            "total_shards": 2,
            "ok_shards": 1,
            "failed_shards": [{"fp_id": "FP-B", "error": "injected"}],
        },
    )
    stats = await agent.persist_to_kb(
        SRR_MIN, [good, unrev], report,
        publication_context={"gate_result": {"passed": True}},
    )

    assert stats["auto_approved"] == 1
    assert stats["pending_review"] == 1
    rows = {o.id: o.status for o in fake.added if type(o).__name__ == "TestCase"}
    assert rows["TC-GOOD"] == "approved"
    assert rows["TC-UNREV"] == "reviewed", "失败分片用例不得落 approved（docs/31 F1）"


@pytest.mark.asyncio
async def test_persist_review_degraded_blocks_whole_batch_auto(monkeypatch):
    """同因延伸（范围外加固）：review_degraded 整批未审，高置信条也不得 auto。"""
    fake = _FakeSession()
    monkeypatch.setattr("app.db.database.async_session", lambda: fake)
    agent = ReviewAgent()
    case = _clean_case("TC-DEG", "FP-A", title="降级批次高置信条", conf=0.95)
    report = _report(
        [case], review_status="degraded",
        review_degraded=True, review_degraded_reason="llm_down",
    )
    stats = await agent.persist_to_kb(
        SRR_MIN, [case], report,
        publication_context={"gate_result": {"passed": True}},
    )

    assert stats["auto_approved"] == 0
    assert stats["pending_review"] == 1
    rows = {o.id: o.status for o in fake.added if type(o).__name__ == "TestCase"}
    assert rows["TC-DEG"] == "reviewed"


@pytest.mark.asyncio
async def test_persist_partial_block_purges_bad_keeps_good(monkeypatch):
    """F6「坏的剔除、好的要保留」：blocker 坏条留档 error_cases，好条照常 auto。"""
    fake = _FakeSession()
    monkeypatch.setattr("app.db.database.async_session", lambda: fake)
    agent = ReviewAgent()
    bad = _clean_case("TC-BAD", "FP-A", title="坏条-内容阻断", conf=0.9)
    good = _clean_case("TC-GOOD2", "FP-A", title="好条-保留入库", conf=0.85)
    report = _report(
        [bad, good],
        issues=[{
            "case_id": "TC-BAD", "dimension": "executability",
            "severity": "blocker", "message": "缺断言",
        }],
    )
    stats = await agent.persist_to_kb(
        SRR_MIN, [bad, good], report,
        publication_context={"gate_result": {"passed": True}},
    )

    assert stats["purged_cases"] == 1
    assert stats["auto_approved"] == 1 and stats["pending_review"] == 0
    err_rows = [o for o in fake.added if type(o).__name__ == "ErrorCase"]
    assert any(o.original_case_id == "TC-BAD" for o in err_rows), "坏条须留档 error_cases"
    rows = {o.id: o for o in fake.added if type(o).__name__ == "TestCase"}
    assert rows["TC-GOOD2"].status == "approved"
    assert rows["TC-BAD"].case_json["publication"]["purged"] is True


@pytest.mark.asyncio
async def test_persist_low_confidence_rejected_still_records(monkeypatch):
    """_persist_route 收敛后 else 分支（低置信 rejected）的 error_id 耦合不破坏。"""
    fake = _FakeSession()
    monkeypatch.setattr("app.db.database.async_session", lambda: fake)

    import app.agents.review_agent as ra_mod

    class _FakeVector:
        def __init__(self):
            self.error_cases = []

        def add_error_case(self, **kw):
            self.error_cases.append(kw)

    fake_vec = _FakeVector()
    monkeypatch.setattr(ra_mod, "vector_store", fake_vec)

    agent = ReviewAgent()
    case = _clean_case("TC-LOW", "FP-A", title="低置信条", conf=0.42)
    report = _report([case])
    stats = await agent.persist_to_kb(
        SRR_MIN, [case], report,
        publication_context={"gate_result": {"passed": True}},
    )

    assert stats["rejected"] == 1 and stats["auto_approved"] == 0
    err_rows = [o for o in fake.added if type(o).__name__ == "ErrorCase"]
    assert any(
        o.original_case_id == "TC-LOW" and o.error_type == "incomplete"
        for o in err_rows
    )
    assert fake_vec.error_cases and fake_vec.error_cases[0]["original_case_id"] == "TC-LOW"


# ============================================================================
# 组 4：orchestrator degraded_flags 透出（B-2 可见性）
# ============================================================================


@pytest.mark.asyncio
async def test_review_node_marks_shard_partial_failure_in_degraded_flags(monkeypatch):
    """review_shard_partial_failure 必须进 degraded_flags，且不被最终 report 覆盖。"""
    from app.agents import review_agent as ra_mod
    from app.core.orchestrator import Orchestrator

    case = _clean_case("TC-X", "FP-B", title="未审分片条", conf=0.9)
    report = _report(
        [case],
        review_status="partial",
        review_shard_partial_failure=True,
        shard_review={
            "total_shards": 2,
            "ok_shards": 1,
            "failed_shards": [{"fp_id": "FP-B", "error": "injected"}],
        },
    )
    review_mock = AsyncMock(return_value=(report, [case]))
    persist_mock = AsyncMock(return_value={"auto_approved": 0, "pending_review": 1})
    originals = (ra_mod.review_agent.review, ra_mod.review_agent.persist_to_kb)
    ra_mod.review_agent.review = review_mock
    ra_mod.review_agent.persist_to_kb = persist_mock
    try:
        orch = Orchestrator()
        update = await orch._review_node({
            "srr": SRR_MIN, "test_cases": [case], "req_id": "REQ-2024-001",
            "channel_config": {}, "impact_result": {}, "functional_points": [],
            "supplement_iteration": 0,
        })
    finally:
        ra_mod.review_agent.review, ra_mod.review_agent.persist_to_kb = originals

    flags = update.get("degraded_flags") or []
    assert "review_shard_partial_failure" in flags
    assert "review_llm_failed" not in flags
    assert update["review_result"]["shard_review"]["failed_shards"][0]["fp_id"] == "FP-B"
    assert update["review_result"]["review_shard_partial_failure"] is True


@pytest.mark.asyncio
async def test_review_node_marks_review_llm_failed_on_degraded(monkeypatch):
    """review_degraded 路径既有标记 review_llm_failed 不回归。"""
    from app.agents import review_agent as ra_mod
    from app.core.orchestrator import Orchestrator

    case = _clean_case("TC-Y", "FP-A", title="降级批次条", conf=0.9)
    report = _report(
        [case], review_status="degraded",
        review_degraded=True, review_degraded_reason="llm_down",
    )
    review_mock = AsyncMock(return_value=(report, [case]))
    persist_mock = AsyncMock(return_value={"auto_approved": 0, "pending_review": 1})
    originals = (ra_mod.review_agent.review, ra_mod.review_agent.persist_to_kb)
    ra_mod.review_agent.review = review_mock
    ra_mod.review_agent.persist_to_kb = persist_mock
    try:
        orch = Orchestrator()
        update = await orch._review_node({
            "srr": SRR_MIN, "test_cases": [case], "req_id": "REQ-2024-001",
            "channel_config": {}, "impact_result": {}, "functional_points": [],
            "supplement_iteration": 0,
        })
    finally:
        ra_mod.review_agent.review, ra_mod.review_agent.persist_to_kb = originals

    assert "review_llm_failed" in (update.get("degraded_flags") or [])


# ============================================================================
# 组 5：generate.py except 分支对称性——done 带两键、失败分支不覆盖
# ============================================================================

_REVIEW_REPORT_EVIDENCE = {
    "total_cases": 1,
    "passed_cases": 1,
    "blocked_cases": 0,
    "issues": [],
    "should_supplement": False,
    "avg_confidence": 0.9,
    "review_status": "partial",
    "shard_review": {
        "total_shards": 2,
        "ok_shards": 1,
        "failed_shards": [{"fp_id": "FP-B", "error": "injected"}],
    },
    "review_shard_partial_failure": True,
    "review_degraded": False,
}


def _done_event(publication: dict) -> dict:
    return {
        "stage": "done",
        "progress": 100,
        "cases": [{"id": "TC-1", "type": "functional", "title": "用例1"}],
        "review_report": dict(_REVIEW_REPORT_EVIDENCE),
        "publication": publication,
        "gate_result": {},
        "kb_flowback": {},
        "point_coverage": {},
        "degraded_flags": ["review_shard_partial_failure"],
    }


async def _drive_sse(events=None, exc=None):
    """驱动 generate_test_cases 的 SSE 生成器。

    Returns:
        (updates, yielded)：_update_gen_task 各次调用的 UPDATE 编译参数、
        SSE data 解析后的事件列表。
    """
    from app.api import generate as gen_api
    from app.models.api_schemas import GenerateRequest

    updates: list[dict] = []
    yielded: list[dict] = []

    session = _FakeSession(select_row=SimpleNamespace(
        id="REQ-2024-001", product_id="PROD-001", raw_text="需求原文",
        srr_json={"requirement": {"id": "REQ-2024-001", "title": "登录"}},
        status="confirmed",
    ))

    async def _run_pipeline(**kwargs):
        for ev in (events or []):
            yield ev
        if exc is not None:
            raise exc

    class _Resp:
        def __init__(self, gen, **kw):
            self.gen = gen

    with patch.object(gen_api, "async_session", lambda: session), \
         patch.object(gen_api, "orchestrator",
                      SimpleNamespace(run_pipeline=_run_pipeline)), \
         patch.object(gen_api, "EventSourceResponse", _Resp), \
         patch.object(gen_api, "assert_product_visible", AsyncMock()), \
         patch("app.core.task_guard.reserve_generation_slot",
               AsyncMock(return_value={"status": "ok", "task_id": "TASK-1"})), \
         patch("app.core.review_reports.save_review_reports", AsyncMock()):
        resp = await gen_api.generate_test_cases(
            GenerateRequest(req_id="REQ-2024-001"), _FakeRequest(), user=object(),
        )
        async for item in resp.gen:
            yielded.append(json.loads(item["data"]))

    updates.extend(session.update_params)
    return updates, yielded


class _FakeRequest:
    async def is_disconnected(self):
        return False


@pytest.mark.asyncio
async def test_done_result_json_carries_shard_evidence():
    """done 正常路径：result_json.review_report 必须带 shard_review 与 partial 键。"""
    updates, _ = await _drive_sse(
        events=[_done_event({"decision": "auto_approve", "reasons": []})]
    )
    with_result = [u for u in updates if "result_json" in u]
    assert len(with_result) == 1
    body = with_result[0]["result_json"]
    assert body["review_report"]["shard_review"]["failed_shards"][0]["fp_id"] == "FP-B"
    assert body["review_report"]["review_shard_partial_failure"] is True
    assert "review_shard_partial_failure" in (body.get("degraded_flags") or [])
    assert with_result[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_done_path_still_carries_shard_evidence():
    """failed 路径同样透出：publication=blocked → status=failed 但两键仍在 result_json。"""
    updates, _ = await _drive_sse(
        events=[_done_event({"decision": "blocked", "reasons": ["empty_generation"]})]
    )
    with_result = [u for u in updates if "result_json" in u]
    assert len(with_result) == 1
    assert with_result[0]["status"] == "failed"
    body = with_result[0]["result_json"]
    assert body["review_report"]["shard_review"] == _REVIEW_REPORT_EVIDENCE["shard_review"]
    assert body["review_report"]["review_shard_partial_failure"] is True


@pytest.mark.asyncio
async def test_error_event_never_clobbers_result_json():
    """error 事件分支：只写失败状态，不得用空 result_json 覆盖既有证据。"""
    updates, yielded = await _drive_sse(events=[{
        "stage": "error", "progress": 100,
        "message": "模拟流水线失败", "req_id": "REQ-2024-001",
    }])
    assert updates, "error 分支应写 failed 状态"
    assert updates[-1]["status"] == "failed"
    assert all("result_json" not in u for u in updates), \
        "error 分支不得写 result_json（防覆盖 checkpoint/审核证据）"
    assert yielded[-1]["stage"] == "error"


@pytest.mark.asyncio
async def test_unhandled_exception_never_clobbers_result_json():
    """except Exception 兜底分支：同样不得覆盖 result_json。"""
    updates, yielded = await _drive_sse(
        events=[{"stage": "progress", "progress": 30, "message": "生成中"}],
        exc=RuntimeError("模拟未捕获异常"),
    )
    assert updates and updates[-1]["status"] == "failed"
    assert all("result_json" not in u for u in updates), \
        "except 兜底分支不得写 result_json（防覆盖 checkpoint/审核证据）"
    assert yielded[-1]["stage"] == "error"
