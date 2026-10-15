"""资源感知并发控制单测（v4.0.2）。

覆盖三类风险，每类都对应一个" Silent 失效"的真实形态：

1. **探测口径**：容器里 `os.cpu_count()` 返回宿主逻辑核，若按它定并发会把
   机器打穿。测 cgroup 配额优先、物理核去超线程、fail-open。
2. **准入语义**：并发上限必须真的拦住第二/N 个任务；release 后必须放行；
   等待超时要明确失败而不是无限挂起。
3. **槽位泄漏**：release 是同步的（理由见 GenerationGate docstring）——
   这里显式验它不需要 await、且在取消后仍能归还。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.core.resource_guard import (  # noqa: E402
    GenerationGate,
    cpu_limit,
    inference_slots,
    physical_cores,
    task_ceiling,
)
from app.core import resource_guard as rg_module  # noqa: E402


# ============================================================================
# 探测口径
# ============================================================================

def test_cpu_limit_is_positive_and_finite():
    """探测失败也不能返回 0/负数——那会让并发预算算出 0，生成永远进不来。"""
    v = cpu_limit()
    assert v > 0
    assert v == float(int(v)) or v > 0  # 允许 2.5 这类配额值


def test_physical_cores_discards_hyperthreading():
    """物理核必须 ≤ 逻辑核，且至少 1（与 milvus_store._physical_cores 同口径）。"""
    p, l = physical_cores(), int(cpu_limit())
    assert 1 <= p <= max(1, l)


def test_inference_slots_reserves_one_core_for_main_process():
    """必须留 1 核给主进程/事件循环——这是并发预算的底线，不是可选项。"""
    assert inference_slots() >= 1
    assert inference_slots() == max(1, physical_cores() - 1)


def test_probes_fall_open_when_sources_missing(monkeypatch):
    """所有探测源都不可用时退到 cpu_count，而不是抛异常/返回 0。"""
    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_cgroup_v1_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_affinity_count", lambda: None)
    import os

    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    assert cpu_limit() == 6.0


def test_cgroup_quota_takes_priority_over_host_cores(monkeypatch):
    """容器配额必须优先于宿主逻辑核——否则 8 核算出的并发会在 2 核配额下打穿。"""
    import os

    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: 2.0)
    monkeypatch.setattr(rg_module, "_affinity_count", lambda: 8)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert cpu_limit() == 2.0
    # 2 核 → 1 物理核 → 推理份额 1
    assert physical_cores() == 1
    assert inference_slots() == 1


# ============================================================================
# 并发预算
# ============================================================================

def test_task_ceiling_bounded_by_slots_and_config(monkeypatch):
    """上限 = min(推理份额, 配置硬顶)，且至少 1。"""
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 3)
    assert task_ceiling() == 3
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 99)
    assert task_ceiling() == 4  # GEN_TASK_CONCURRENCY_MAX
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 0)
    assert task_ceiling() == 1  # 绝不为 0


def test_task_ceiling_derates_on_cpu_overload(monkeypatch):
    """CPU 打满且负载超核数 → 降一档（机器已经在喘，别再往上压）。"""
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 4)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(
        rg_module, "_sampler",
        type("S", (), {"snapshot": staticmethod(lambda: {
            "cpu_percent_avg": 95.0, "load_1m": 6.0,
            "memory": {"available_ratio": 0.5},
        })})(),
    )
    assert task_ceiling() == 3


def test_task_ceiling_floors_to_one_on_low_memory(monkeypatch):
    """可用内存过低 → 压到 1。OOM Kill 比慢更致命，没有商量余地。"""
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 4)
    monkeypatch.setattr(
        rg_module, "_sampler",
        type("S", (), {"snapshot": staticmethod(lambda: {
            "cpu_percent_avg": 10.0, "load_1m": 0.1,
            "memory": {"available_ratio": 0.05},
        })})(),
    )
    assert task_ceiling() == 1


# ============================================================================
# 准入语义
# ============================================================================

@pytest.fixture
def fixed_gate(monkeypatch):
    """限额恒为 1 的门：第二个任务必须排队。"""
    g = GenerationGate()
    monkeypatch.setattr(rg_module, "task_ceiling", lambda snap=None: 1)
    return g


@pytest.mark.asyncio
async def test_second_task_waits_until_release(fixed_gate):
    """限额 1 时第二个任务必须阻塞，直到第一个 release。"""
    assert (await fixed_gate.acquire("t1"))["admitted"] is True
    assert fixed_gate.active_tasks == 1

    async def _second():
        return await fixed_gate.acquire("t2", max_wait_s=5.0)

    t2 = asyncio.create_task(_second())
    await asyncio.sleep(0.05)
    assert not t2.done(), "限额未满就该阻塞等待，而不是直接拒绝"
    assert fixed_gate.waiting_count == 1

    fixed_gate.release("t1")
    r = await asyncio.wait_for(t2, timeout=2.0)
    assert r["admitted"] is True
    assert fixed_gate.active_tasks == 1
    assert fixed_gate.waiting_count == 0
    assert r["waited_ms"] > 0


@pytest.mark.asyncio
async def test_wait_timeout_returns_explicit_failure(fixed_gate):
    """等不到资源必须**明确失败**——不能让用户对着永远不动的页面干等。"""
    assert (await fixed_gate.acquire("t1"))["admitted"] is True
    r = await fixed_gate.acquire("t2", max_wait_s=0.2)
    assert r["admitted"] is False
    assert r["reason"] == "wait_timeout"
    assert fixed_gate.waiting_count == 0  # 超时后必须把自己移出队列
    assert fixed_gate.active_tasks == 1   # 且不能顺手把别人的槽位放掉


@pytest.mark.asyncio
async def test_queue_full_rejects(fixed_gate):
    """排队者超上限直接拒绝，防止请求无限堆积把连接池也占满。"""
    await fixed_gate.acquire("t1")
    waiters = [asyncio.create_task(fixed_gate.acquire(f"w{i}", max_wait_s=5.0)) for i in range(8)]
    await asyncio.sleep(0.05)
    r = await fixed_gate.acquire("overflow", max_wait_s=1.0)
    assert r["admitted"] is False
    assert r["reason"] == "queue_full"
    for w in waiters:
        w.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)


@pytest.mark.asyncio
async def test_abort_check_abandons_queue(fixed_gate):
    """客户端断连要及时放弃排队——用户关了页面不该还白占一个等待位。"""
    await fixed_gate.acquire("t1")
    gone = {"v": False}

    async def _flip():
        await asyncio.sleep(0.05)
        gone["v"] = True

    asyncio.create_task(_flip())
    r = await fixed_gate.acquire("t2", max_wait_s=5.0, abort_check=lambda: gone["v"])
    assert r["admitted"] is False
    assert r["reason"] == "client_gone"


@pytest.mark.asyncio
async def test_release_is_synchronous_and_idempotent(fixed_gate):
    """release 必须是同步方法（SSE 取消作用域里 await 会立刻再抛 Cancelled），
    且重复释放/释放未持有的任务都不能炸。"""
    assert (await fixed_gate.acquire("t1"))["admitted"] is True
    fixed_gate.release("t1")
    fixed_gate.release("t1")          # 幂等
    fixed_gate.release("never-held")  # 释放没拿过的任务
    assert fixed_gate.active_tasks == 0
    # 释放后立即可再进
    assert (await fixed_gate.acquire("t3"))["admitted"] is True


@pytest.mark.asyncio
async def test_release_wakes_waiters_without_polling_delay(fixed_gate):
    """release 应立即唤醒等待者（Event 机制），不必等 2s 轮询兜底。"""
    await fixed_gate.acquire("t1")
    t2 = asyncio.create_task(fixed_gate.acquire("t2", max_wait_s=5.0))
    await asyncio.sleep(0.05)
    t0 = asyncio.get_running_loop().time()
    fixed_gate.release("t1")
    await asyncio.wait_for(t2, timeout=1.0)
    assert asyncio.get_running_loop().time() - t0 < 0.5, "唤醒应近实时，而非等到轮询超时"


# ============================================================================
# FP 并发收敛
# ============================================================================

def test_clamp_fp_concurrency_no_regression_single_task(monkeypatch):
    """**无回归保证**：单任务时 FP 并发度必须等于用户配置值。"""
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 3)
    g = GenerationGate()
    assert g.clamp_fp_concurrency(3) == 3
    assert g.clamp_fp_concurrency(2) == 2
    assert g.clamp_fp_concurrency(5) == 3  # 收敛到预算内


def test_clamp_fp_concurrency_shares_budget_across_tasks(monkeypatch):
    """多任务时自动平分推理份额：3 份额 2 任务 → 各 1。"""
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 3)
    g = GenerationGate()
    g._active = ["a"]           # 1 个活跃任务 → 拿全部份额（无回归）
    assert g.clamp_fp_concurrency(3) == 3
    g._active = ["a", "b"]      # 2 个 → 平分
    assert g.clamp_fp_concurrency(3) == 1  # 3//2=1，不会降到 0
    g._active = ["a", "b", "c", "d"]
    assert g.clamp_fp_concurrency(3) == 1  # 份额再小也保底 1


def test_try_acquire_does_not_enqueue():
    """快路径失败不得进等待队列——否则单用户场景也会无谓占位。"""
    monkeypatch_saved = rg_module.task_ceiling
    rg_module.task_ceiling = lambda snap=None: 0  # 永不满也永不空
    try:
        g = GenerationGate()
        assert g.try_acquire("t1") is False
        assert g.waiting_count == 0
        assert g.active_tasks == 0
    finally:
        rg_module.task_ceiling = monkeypatch_saved


# ============================================================================
# 单例门面
# ============================================================================

def test_singleton_health_brief_is_cheap_and_consistent():
    """health_brief 只读内存计数（不读 /proc），字段必须自洽。"""
    b = rg_module.resource_guard.health_brief()
    assert b["inference_slots"] >= 1
    assert b["task_ceiling"] >= 1
    assert b["active_tasks"] >= 0
    assert b["waiting_tasks"] >= 0
    assert b["cpu_physical_cores"] >= 1


def test_stats_shape_matches_frontend_contract():
    """stats() 的字段结构是前端 SystemResources 类型的契约，改动即破坏页面。"""
    s = rg_module.resource_guard.stats()
    for key in ("cpu", "memory", "inference_slots", "sampled_at", "generation"):
        assert key in s, f"stats() 缺少字段 {key}（前端 Dashboard 会读到 undefined）"
    for key in ("logical_cores", "physical_cores", "cpu_limit", "usage_percent",
                "usage_percent_avg", "load_1m", "load_5m", "load_15m"):
        assert key in s["cpu"], f"cpu 缺少字段 {key}"
    for key in ("total_mb", "available_mb", "available_ratio", "source"):
        assert key in s["memory"], f"memory 缺少字段 {key}"
    for key in ("active", "waiting", "ceiling", "admitted_total", "rejected_total",
                "max_wait_ms", "last_wait_ms", "last_reject_reason", "active_task_ids"):
        assert key in s["generation"], f"generation 缺少字段 {key}"


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-q"]))
