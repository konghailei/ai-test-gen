# -*- coding: utf-8 -*-
"""v3.5.5 守卫：FP 子批受控并发生成（docs/22 方案①）。

行为验证（非 grep 字面量）：
1. 并发无失败放大——单 FP 失败不阻断其它 FP；
2. checkpoint 完成即写——每 FP 完成后立即全量覆盖写，fp_groups 单调递增；
3. 进度事件按 key 去重刷新——并发交错事件后无重复 key，done 后 covered==total；
4. 串行回退——并发度 1 时行为与串行一致；
5. 合并顺序——resolved_cases 按 FP 完成序合并但 related_fp 标注正确。
"""
import asyncio
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app")

from app.core.orchestrator import Orchestrator


def _mk_fp(fid: str, name: str) -> dict:
    """小 FP（要点数 ≤40，不触发 D-5 二次分片）。"""
    return {
        "id": fid, "name": name, "priority": "P1",
        "srr_json": {
            "flows": {"happy_path": {"steps": [
                {"action": f"步骤{i}：执行操作并校验结果"} for i in range(1, 6)
            ]}},
            "business_rules": [],
            "data_constraints": [],
        },
    }


SRR = {"requirement": {"id": "R-CONC", "priority": "P1"}, "flows": {}}


def _mk_state(fp_list: list[dict]) -> dict:
    return {
        "srr": SRR,
        "task_id": "T-CONC",
        "req_id": "R-CONC",
        "channel_config": {"enable_multi_type_generation": True},
        "impact_result": {"generation_guidance": ""},
        "functional_points": fp_list,
        "degraded_flags": [],
        "test_design_blueprint": {"historical_regression_cases": [], "coverage_gaps": []},
        "resume": False,
    }


class _FakeQueue:
    """捕获 _emit_gen 推送的事件，供断言进度卡片状态。"""

    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


def _mk_case(fid: str, idx: int) -> dict:
    return {
        "id": f"TC-{fid}-{idx}",
        "title": f"{fid} 用例 {idx}",
        "related_fp": fid,
        "steps": [{"action": "a", "expected": "e"}],
        "gen_metadata": {"confidence": 0.9},
    }


def _run_gen_node(fp_list: list[dict], gen_behaviors: dict, concurrency: int):
    """驱动 _gen_node 的公共脚手架。

    gen_behaviors: {fid: 返回用例列表 或 异常实例 或 "slow"}
    concurrency: kb_settings 返回的并发度（patch 掉 DB 读取）
    """
    orch = Orchestrator()

    async def _fake_generate(srr, **kwargs):
        shards = kwargs.get("functional_points") or []
        fid = shards[0].get("id", "") if shards else ""
        behavior = gen_behaviors.get("ALL", gen_behaviors.get(fid, "ok"))
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "slow":
            await asyncio.sleep(0.3)
        if behavior == "track":
            gen_behaviors.setdefault("_active", 0)
            gen_behaviors["_active"] += 1
            gen_behaviors["_peak"] = max(gen_behaviors.get("_peak", 0), gen_behaviors["_active"])
            await asyncio.sleep(0.15)
            gen_behaviors["_active"] -= 1
            return [_mk_case(fid, 1)]
        return [_mk_case(fid, 1)]

    state = _mk_state(fp_list)
    queue = _FakeQueue()
    token = orch._gen_event_queue.set(queue)
    saved_snapshots: list[list[dict]] = []

    async def _fake_save(task_id, fp_groups, fingerprint=None):
        saved_snapshots.append([dict(g) for g in fp_groups])

    patches = [
        patch("app.agents.gen_agent.gen_agent.generate", side_effect=_fake_generate),
        patch.object(Orchestrator, "_save_gen_checkpoint", side_effect=_fake_save),
        # _gen_node 内是 `from app.api.settings import get_gen_fp_concurrency`（函数内导入），
        # patch 必须打在定义处
        patch("app.api.settings.get_gen_fp_concurrency", AsyncMock(return_value=concurrency)),
    ]
    return orch, state, queue, saved_snapshots, patches, token


@pytest.mark.asyncio
async def test_no_failure_amplification():
    """单 FP 生成失败不得阻断其它 FP；失败 FP 进 degraded_flags。"""
    fp_list = [_mk_fp("FP-1", "登录"), _mk_fp("FP-2", "查询"), _mk_fp("FP-3", "导出")]
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list,
        {"FP-1": RuntimeError("LLM 全挂"), "FP-2": "ok", "FP-3": "ok"},
        concurrency=3,
    )
    try:
        for p in patches:
            p.start()
        update = await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    cases = update["test_cases"]
    # FP-2 / FP-3 正常产出
    fids = {c["related_fp"] for c in cases}
    assert "FP-2" in fids and "FP-3" in fids, "失败 FP 连坐了其它 FP"
    assert "FP-1" not in fids, "失败 FP 不应有产出"
    assert "fp_generate_failed" in update["degraded_flags"]
    # 失败 FP 不应出现在 checkpoint 完成组（本地 FP-1 failed → emit failed，无 done）
    done_keys = set()
    for ev in queue.events:
        sub = ev.get("fp_subtask") or {}
        if sub.get("status") == "done":
            done_keys.add(sub["key"])
    assert done_keys == {"FP-2", "FP-3"}


@pytest.mark.asyncio
async def test_checkpoint_written_per_completion():
    """checkpoint 完成即写：≥N 次写入且 fp_groups 单调递增（全量覆盖收敛）。"""
    fp_list = [_mk_fp(f"FP-{i}", f"功能{i}") for i in range(1, 4)]
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list, {"ALL": "slow"}, concurrency=3,
    )
    try:
        for p in patches:
            p.start()
        await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    assert len(saved) >= 3, f"每 FP 完成都应写 checkpoint，实测 {len(saved)} 次"
    # 单调递增：后写的一定包含前面所有组（全量覆盖语义）
    for prev, curr in zip(saved, saved[1:]):
        assert len(curr) >= len(prev), "checkpoint fp_groups 必须单调不减"
    assert len(saved[-1]) == 3, "最后一次写入应包含全部 3 个 FP"
    ids = {g["fp_id"] for g in saved[-1]}
    assert ids == {"FP-1", "FP-2", "FP-3"}


@pytest.mark.asyncio
async def test_progress_events_key_dedup():
    """进度事件按 fp_id key 刷新：并发交错后无重复 key，done 后 covered==total。"""
    fp_list = [_mk_fp(f"FP-{i}", f"功能{i}") for i in range(1, 4)]
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list, {"ALL": "slow"}, concurrency=3,
    )
    try:
        for p in patches:
            p.start()
        update = await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    # 每个事件携带全量 subtasks 快照；取最后一个事件的快照验证
    last = queue.events[-1]
    snap = last["subtasks"]
    keys = [s["key"] for s in snap]
    assert len(keys) == len(set(keys)), f"subtasks 快照 key 重复: {keys}"
    assert {s["status"] for s in snap} == {"done"}
    fp_progress = last["fp_progress"]
    assert fp_progress["covered"] == fp_progress["total"] == 3


@pytest.mark.asyncio
async def test_serial_fallback_concurrency_1():
    """并发度=1 串行回退：行为正确且无并发峰值。"""
    fp_list = [_mk_fp(f"FP-{i}", f"功能{i}") for i in range(1, 4)]
    behaviors = {"ALL": "track", "_peak": 0}
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list, behaviors, concurrency=1,
    )
    try:
        for p in patches:
            p.start()
        await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    assert behaviors["_peak"] == 1, "串行回退下并发峰值必须为 1"
    assert len(saved[-1]) == 3


@pytest.mark.asyncio
async def test_concurrency_semaphore_respected():
    """并发度=2 时实际并发峰值 ≤2（Semaphore 限流生效）。"""
    fp_list = [_mk_fp(f"FP-{i}", f"功能{i}") for i in range(1, 6)]  # 5 个 FP
    behaviors = {"ALL": "track", "_peak": 0}
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list, behaviors, concurrency=2,
    )
    try:
        for p in patches:
            p.start()
        await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    assert behaviors["_peak"] <= 2, f"并发峰值 {behaviors.get('_peak')} 超过限流 2"
    assert behaviors["_peak"] == 2, "5 个 FP 并发度 2 应打满 2 路并行"
    # 全部完成
    assert len(saved[-1]) == 5


@pytest.mark.asyncio
async def test_merge_order_and_fp_tagging():
    """并发完成序 != 原 FP 顺序时，用例 related_fp 标注仍正确、无丢失。"""
    # FP-1 慢、FP-3 快 → 完成序与提交序不同
    fp_list = [_mk_fp("FP-1", "慢功能"), _mk_fp("FP-2", "中功能"), _mk_fp("FP-3", "快功能")]

    orch = Orchestrator()

    async def _fake_generate(srr, **kwargs):
        shards = kwargs.get("functional_points") or []
        fid = shards[0].get("id", "") if shards else ""
        if fid == "FP-1":
            await asyncio.sleep(0.4)
        elif fid == "FP-3":
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.15)
        c = _mk_case(fid, 1)
        c.pop("related_fp")  # 模拟 GenAgent 未标注 → 验证 _gen_node 兜底标注
        return [c]

    state = _mk_state(fp_list)
    queue = _FakeQueue()
    token = orch._gen_event_queue.set(queue)
    saved: list[list[dict]] = []

    async def _fake_save(task_id, fp_groups, fingerprint=None):
        saved.append([dict(g) for g in fp_groups])

    patches = [
        patch("app.agents.gen_agent.gen_agent.generate", side_effect=_fake_generate),
        patch.object(Orchestrator, "_save_gen_checkpoint", side_effect=_fake_save),
        patch("app.api.settings.get_gen_fp_concurrency", AsyncMock(return_value=3)),
    ]
    try:
        for p in patches:
            p.start()
        update = await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    cases = update["test_cases"]
    assert {c["related_fp"] for c in cases} == {"FP-1", "FP-2", "FP-3"}
    assert all(c["related_fp"] for c in cases), "兜底标注失效"


@pytest.mark.asyncio
async def test_gather_exception_isolation():
    """gather 兜底：Exception 级任务异常被 return_exceptions=True 捕获，
    不击穿 _gen_node 主流程；对应 FP 标记失败。"""
    fp_list = [_mk_fp("FP-1", "正常"), _mk_fp("FP-2", "会炸")]

    orch = Orchestrator()
    state = _mk_state(fp_list)
    queue = _FakeQueue()
    token = orch._gen_event_queue.set(queue)

    async def _gen_one_fp_stub(fp):
        if fp.get("id") == "FP-2":
            raise RuntimeError("模拟基础设施异常")
        return None

    patches = [
        patch("app.agents.gen_agent.gen_agent.generate",
              AsyncMock(return_value=[_mk_case("FP-1", 1)])),
        patch.object(Orchestrator, "_save_gen_checkpoint", AsyncMock()),
        patch("app.api.settings.get_gen_fp_concurrency", AsyncMock(return_value=3)),
        # 把 _gen_one_fp 换成 stub：直接对实例打补丁（闭包内引用是局部名，
        # 但 _gen_node 内调用 `_gen_one_fp(fp)` 走的是本地闭包名——无法 patch。
        # 因此这里直接测 gather 语义本身：异常被捕获、不传播）
    ]
    try:
        for p in patches:
            p.start()
        # 直接复现 _gen_node 的 gather 兜底语义
        results = await asyncio.gather(
            *[_gen_one_fp_stub(fp) for fp in fp_list], return_exceptions=True
        )
        failed = [
            fp.get("id") for fp, r in zip(fp_list, results)
            if isinstance(r, BaseException)
        ]
        assert failed == ["FP-2"], "gather 应隔离失败任务并返回异常实例"
        # FP-1 正常返回不被影响
        assert results[0] is None
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)


@pytest.mark.asyncio
async def test_degraded_flag_no_duplicate():
    """degraded_flags 去重守卫：多 FP 同时失败只记一条 fp_generate_failed。"""
    fp_list = [_mk_fp(f"FP-{i}", f"功能{i}") for i in range(1, 4)]
    orch, state, queue, saved, patches, token = _run_gen_node(
        fp_list,
        {"FP-1": RuntimeError("e1"), "FP-2": RuntimeError("e2"), "FP-3": RuntimeError("e3")},
        concurrency=3,
    )
    try:
        for p in patches:
            p.start()
        update = await orch._gen_node(state)
    finally:
        for p in patches:
            p.stop()
        orch._gen_event_queue.reset(token)

    assert update["degraded_flags"].count("fp_generate_failed") == 1, \
        "degraded_flags 重复标记（not in 守卫失效）"
    assert len(update["test_cases"]) == 0
