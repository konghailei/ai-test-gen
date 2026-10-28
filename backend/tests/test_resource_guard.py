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
    """大机器上必须留 1 核给主进程/事件循环——这是并发预算的底线。"""
    assert inference_slots() >= 1


def test_inference_slots_small_machine_keeps_one(monkeypatch):
    """只有 1 个物理核时**不扣**主进程那一核。

    小机器上"扣 → 0 → 兜成 1"等于没扣，却让人以为留了一核给事件循环；
    而 api 在等 LLM 返回时实测只吃 1~1.6% CPU，那核留着是纯浪费。
    """
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 1)
    assert rg_module.inference_slots() == 1, "1 物理核不能被扣成 0（也不该假装扣过）"


def test_inference_slots_ignores_peer_projection(monkeypatch):
    """**slots 不扣兄弟投影**（v4.1.7 返工结论，真实回归）。

    为什么不能扣：slots 还被 `clamp_fp_concurrency()` 用来算**单任务内的 FP
    并行度**，而 celery 是异步旁路、大部分时间空闲。用它永久砍掉 api 的并行度
    是常态性浪费。

    实测代价（本机 8 逻辑核 / api cpus=6 / celery cpus=3）：
        扣 → slots=1 → FP 并行度 1 → 生成慢一倍
        不扣 → slots=2 → FP 并行度 2（与 v4.1.6 一致，无回归）
    这条断言锁住"不扣"；投影扣减的位置见下一条（task_ceiling）。
    """
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "_read_peer_cpu_budget", lambda: (3.0, "test"))
    assert rg_module.inference_slots() == 3, "投影不得影响 inference_slots（只影响 task_ceiling）"


def test_task_ceiling_peer_deduction_units_are_consistent(monkeypatch):
    """投影扣减的单位必须统一：**两边都折算成物理核再相减**。

    v4.1.6 的 bug：`peer`（逻辑核）直接与 `physical_cores()`（物理核）混算，
    而且减完还又 `-1`（与 slots 内部的"留 1 核"重复）。

    夹具 = 真实内网机器：6 逻辑核 / 每核 2 线程（3 物理核）、celery 投影 3 逻辑核。
        slots = reserve(3) = 2
        投影：own_phys=3，peer_phys=floor(3/2)=1 → eff_phys=2
        ceiling = min(2, 2) = **2**
        ❌ v4.1.6：eff_phys=ceil((6−3)/2)=2，再 −1 → 1（少一半吞吐）
    """
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 3)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    monkeypatch.setattr(rg_module, "_read_peer_cpu_budget", lambda: (3.0, "test"))
    monkeypatch.setattr(rg_module, "_cfg", lambda n, d: 4 if n == "GEN_TASK_CONCURRENCY_MAX" else d)
    snap = {"cpu_percent_avg": 0.0, "load_1m": 0.0, "memory": {"available_ratio": 0.9}}
    assert rg_module.task_ceiling(snap) == 2, "投影后应保留 2 个并发，而非被重复扣成 1"


def test_inference_slots_not_double_deducted(monkeypatch):
    """slots 自身不因投影少算（v4.1.6 把"留 1 核"扣了两次）。

    夹具：4 物理核 → slots = 4−1 = 3，无论有没有投影都应是 3。
    """
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "_read_peer_cpu_budget", lambda: (2.0, "test"))
    assert rg_module.inference_slots() == 3


def test_physical_cores_from_logical_rounds_up(monkeypatch):
    """自己的份额必须向上取整：算的是"能承载几个任务"，白丢半个核更亏。"""
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    assert rg_module.physical_cores_from_logical(6) == 3
    assert rg_module.physical_cores_from_logical(3) == 2, "3/2=1.5 应向上取整为 2"
    assert rg_module.physical_cores_from_logical(1) == 1
    assert rg_module.physical_cores_from_logical(0) == 1, "绝不返回 0"


def test_physical_cores_claimed_rounds_down(monkeypatch):
    """兄弟占用必须向下取整：只有"至少占满"的整核才算真被拿走。

    若这里也用 ceil，`ceil_phys(6)=3` 与 `ceil_phys(3)=2` 相加超过整机 3 个
    物理核 → 超卖；实测后果是 6 核机器上并发被压到 1。
    """
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    assert rg_module.physical_cores_claimed(3) == 1, "1.5 个物理核只算占掉 1 个整核"
    assert rg_module.physical_cores_claimed(2) == 1
    assert rg_module.physical_cores_claimed(4) == 2
    assert rg_module.physical_cores_claimed(0) == 0, "无投影必须是 0（不能是 1）"


def test_reserve_for_main_process_skips_on_single_core(monkeypatch):
    """留核规则：1 核不扣（扣了等于没扣），≥2 核扣 1。"""
    assert rg_module._reserve_for_main_process(1) == 1
    assert rg_module._reserve_for_main_process(2) == 1
    assert rg_module._reserve_for_main_process(4) == 3
    assert rg_module._reserve_for_main_process(8) == 7


def test_probes_fall_open_when_sources_missing(monkeypatch):
    """所有探测源都不可用时退到 cpu_count，而不是抛异常/返回 0。

    注意：`_cpuset_count` 也必须 mock —— 它是 affinity 之后、cpu_count 之前
    新增的兜底层，漏 mock 会读到真实 cgroup 文件，让"所有源都缺失"的前提不成立。
    """
    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_cgroup_v1_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_affinity_count", lambda: None)
    monkeypatch.setattr(rg_module, "_cpuset_count", lambda: None)
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
    """CPU 打满且负载超**逻辑核数** → 降一档（机器已经在喘，别再往上压）。

    比较基准 v4.1.5 起是 `cpu_limit()`（逻辑核），不再是物理核——
    故这里必须同时把 cpu_limit mock 成 4，才是"load 6.0 真超了"的场景。
    """
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 4)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 4.0)
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
# 内存探测：cgroup v2 / v1 双通道（v4.1.5，Kernel 3.10 兼容）
# ----------------------------------------------------------------------------
# 这几条是**规则层确定性产出**，必须强断言（不是软断言）：
# 3.10 上只有 cgroup v1，若 `_mem_from_cgroup_v1` 失效，守卫会静默退化为读宿主
# meminfo，`MEM_LOW_RATIO` 的 OOM 保护形同虚设 —— 这是"静默失败"，不会有异常。
# ============================================================================

def _host_total_bytes() -> int | None:
    """宿主总内存（字节）——用于构造"比宿主小"的伪造上限，绕过交叉校验。"""
    pr = rg_module._mem_from_proc()
    return pr[0] if pr else None


def _safe_limit(want: int) -> int:
    """把期望上限压到宿主内存的一半，避免触发"上限 >= 宿主即视为 unlimited"的校验。

    真实容器上限必然小于宿主内存，测试夹具也必须遵守这个物理约束，
    否则测的就不是真实形态（夹具失真会掩盖缺陷）。
    """
    host = _host_total_bytes()
    if host:
        return min(want, host // 2)
    return want


def _fake_v1_dir(tmp_path, limit: int, usage: int, inactive_file: int | None = None):
    """在 tmp 目录伪造 cgroup v1 memory 控制器的文件布局。"""
    d = tmp_path / "memory"
    d.mkdir()
    (d / "memory.limit_in_bytes").write_text(str(limit), encoding="utf-8")
    (d / "memory.usage_in_bytes").write_text(str(usage), encoding="utf-8")
    if inactive_file is not None:
        (d / "memory.stat").write_text(
            f"cache 12345\n"
            f"total_inactive_file {inactive_file}\n"
            f"inactive_file {inactive_file}\n"
            f"rss 999\n",
            encoding="utf-8",
        )
    return str(d)


def test_cgroup_v1_memory_is_read_when_v2_absent(tmp_path):
    """3.10 核心场景：v2 不存在时，v1 必须给出容器真实上限，而不是 None。

    None 会让上层退到 /proc/meminfo（宿主内存）→ OOM 保护失效。
    """
    limit = _safe_limit(1024**3)
    base = _fake_v1_dir(tmp_path, limit=limit, usage=limit // 2)
    got = rg_module._mem_from_cgroup_v1(base)
    assert got is not None, "v1 环境下内存探测返回 None → 会退化为宿主 meminfo"
    total, avail = got
    assert total == limit
    assert avail == limit - limit // 2


def test_cgroup_v1_unlimited_sentinel_returns_none(tmp_path):
    """v1 的"无限制"是巨大数值而非字符串 "max"，必须识别成 None。

    若不识别，9.2EB 会被当成真实上限，ratio≈1.0 → 报告"内存极度充足"，
    比直接失败更危险（假通过）。
    """
    base = _fake_v1_dir(tmp_path, limit=(1 << 63) - 1, usage=1024**3)
    assert rg_module._mem_from_cgroup_v1(base) is None


def test_cgroup_v1_32bit_unlimited_is_detected(tmp_path):
    """32 位 3.10 的 unlimited ≈ 2.1GB，量级上无法区分，必须靠精确等式识别。

    这是"夹具失真会掩盖缺陷"的实证案例：若只按 1<<60 判哨兵，
    2.1GB 会被当成真实上限 → 占用 300MB 时 ratio 已 0.86，稍涨即跌破 0.15
    → 并发被压到 1、排队超时，而 mem_source 仍显示 cgroup_v1，看起来一切正常。
    """
    # 内核真实值：(LONG_MAX & PAGE_MASK) = 0x7FFFF000 = 2147479552
    base = _fake_v1_dir(tmp_path, limit=2147479552, usage=300000000)
    assert rg_module._mem_from_cgroup_v1(base) is None, (
        "32 位 unlimited 哨兵(2147479552) 未被识别 → 会被当成真实的 2GB 上限"
    )


def test_cgroup_v1_32bit_real_limit_not_confused_with_sentinel(tmp_path):
    """反向验证：真实的 2GB 上限（round 值）不能被误判成 unlimited 哨兵。"""
    real_2g = 2 * 1024**3  # 2147483648，与哨兵 2147479552 差 4096（一页）
    got = rg_module._mem_from_cgroup_v1(_fake_v1_dir(tmp_path, limit=real_2g, usage=1024**3))
    assert got is not None, "真实 2GB 上限被误判为 unlimited → OOM 保护会失效"
    assert got[0] == real_2g


def test_cgroup_v1_limit_not_below_host_total(tmp_path):
    """上限 >= 宿主内存在物理上不可能，出现即视为 unlimited（兜底交叉校验）。"""
    host = _host_total_bytes()
    if host is None:
        pytest.skip("无法读取宿主内存，跳过交叉校验用例")
    base = _fake_v1_dir(tmp_path, limit=host, usage=host // 4)
    assert rg_module._mem_from_cgroup_v1(base) is None


def test_cgroup_v1_working_set_excludes_reclaimable_page_cache(tmp_path):
    """可回收页缓存（inactive_file）不算真实占用；available 取保守侧。

    usage 含大量 page cache 时，用 limit-usage 会低估可用内存 → 守卫过早降并发。
    这里验证 working_set 修正生效：available 取 total-working_set 与 limit-usage 的较小者。
    """
    total = _safe_limit(4 * 1024**3)
    usage = int(total * 0.75)            # 占用 75%，其中 25% 是可回收 page cache
    inactive = int(total * 0.25)
    base = _fake_v1_dir(tmp_path, limit=total, usage=usage, inactive_file=inactive)
    got = rg_module._mem_from_cgroup_v1(base)
    assert got is not None
    t, avail = got
    assert t == total
    # working_set = usage - inactive = 50% → avail = min(total-usage, total-working_set)
    assert avail == min(total - usage, total - (usage - inactive))
    assert 0 < avail <= total


def test_cgroup_v1_missing_files_returns_none(tmp_path):
    """非 v1 环境（文件缺失）必须返回 None 交棒给下一个探测源，不能抛异常。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert rg_module._mem_from_cgroup_v1(str(empty)) is None
    assert rg_module._mem_from_cgroup_v1(str(tmp_path / "nope")) is None


def test_mem_snapshot_source_labels_cgroup_version(tmp_path, monkeypatch):
    """快照的 source 必须能区分 cgroup_v1 / cgroup_v2 / proc。

    这是部署侧判断"OOM 保护有没有依据"的唯一观测点：
    source=proc 意味着读的是宿主内存，3.10 上若出现它说明 v1 分支没生效。
    """
    total = _safe_limit(1024**3)
    base = _fake_v1_dir(tmp_path, limit=total, usage=total // 2)

    # 模拟 3.10：v2 读不到，只有 v1
    _real_v1 = rg_module._mem_from_cgroup_v1  # 先存原函数，否则 lambda 会递归调到自身
    monkeypatch.setattr(rg_module, "_mem_from_cgroup_v2", lambda: None)
    monkeypatch.setattr(rg_module, "_mem_from_cgroup_v1", lambda *a, **k: _real_v1(base))

    snap = rg_module._mem_snapshot_raw()
    assert snap["source"] == "cgroup_v1", f"3.10 场景 source 应为 cgroup_v1，实际 {snap['source']}"
    assert snap["total_mb"] == total // 1048576
    assert 0.0 < snap["available_ratio"] <= 1.0


def test_health_brief_exposes_mem_source_for_oom_diagnosis():
    """health_brief 必须带出 mem_source，否则部署后无法判断 OOM 保护是否有效。"""
    b = rg_module.resource_guard.health_brief()
    assert "mem_source" in b, "health_brief 缺少 mem_source（部署侧无法诊断 OOM 保护）"
    assert b["mem_source"] in ("cgroup_v1", "cgroup_v2", "proc", "unknown")


# ============================================================================
# CPU 探测：cgroup v1 quota / cpuset 兜底 / 口径统一（v4.1.5）
# ============================================================================
# 与内存同理：CPU 侧算错不会报错，只会让 task_ceiling 悄悄放大或缩小，
# 表现为"偶发超时/机器被打穿"或"明明有空闲核却只跑一个任务"，很难归因。

def _fake_v1_cpu_dir(tmp_path, quota: int, period: int = 100000):
    d = tmp_path / "cpu"
    d.mkdir()
    (d / "cpu.cfs_quota_us").write_text(str(quota), encoding="utf-8")
    (d / "cpu.cfs_period_us").write_text(str(period), encoding="utf-8")
    return str(d)


def test_cgroup_v1_cpu_quota_is_read(tmp_path):
    """3.10 核心场景：v1 CPU 配额必须给出正确核数（1.5 核这种小数也要保留）。"""
    base = _fake_v1_cpu_dir(tmp_path, quota=150000, period=100000)
    assert rg_module._cgroup_v1_cpu_limit(base) == 1.5


def test_cgroup_v1_cpu_unlimited_returns_none(tmp_path):
    """quota=-1 是 v1 的"无限制"，必须返回 None（否则会算出负数核）。"""
    base = _fake_v1_cpu_dir(tmp_path, quota=-1)
    assert rg_module._cgroup_v1_cpu_limit(base) is None


def test_cgroup_v1_cpu_missing_files_returns_none(tmp_path):
    """非 v1 环境必须返回 None 交棒，不能抛异常拖垮启动。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert rg_module._cgroup_v1_cpu_limit(str(empty)) is None


def test_count_cpu_list_parses_ranges_and_gaps():
    """"0-3,8,10-11" 是 cpuset 的标准写法（含区间+单核混合），必须算出核数。

    算错会直接改变 inference_slots → task_ceiling，属于确定性产出，强断言。
    """
    assert rg_module._count_cpu_list("0") == 1
    assert rg_module._count_cpu_list("0-7") == 8
    assert rg_module._count_cpu_list("0-3,8,10-11") == 7   # 4 + 1 + 2
    assert rg_module._count_cpu_list("0,2,4,6") == 4
    assert rg_module._count_cpu_list("0-3\n8-11") == 8     # 内核会用换行续写
    assert rg_module._count_cpu_list("") is None
    assert rg_module._count_cpu_list("abc") is None


def test_cpuset_count_reads_effective_file_first(tmp_path):
    """v2 优先读 cpuset.cpus.effective（内核算好的），读不到才退 v1 路径。"""
    d = tmp_path / "cg"
    d.mkdir()
    (d / "cpuset.cpus.effective").write_text("0-3", encoding="utf-8")
    assert rg_module._cpuset_count(str(d)) == 4

    d2 = tmp_path / "cg2"
    (d2 / "cpuset").mkdir(parents=True)
    (d2 / "cpuset" / "cpuset.cpus").write_text("0-1,4", encoding="utf-8")
    assert rg_module._cpuset_count(str(d2)) == 3


def test_cpuset_count_returns_none_when_absent(tmp_path):
    d = tmp_path / "cg3"
    d.mkdir()
    assert rg_module._cpuset_count(str(d)) is None


def test_physical_cores_same_caliber_as_milvus_store(monkeypatch):
    """两处物理核口径必须一致（函数注释里的历史约定，此前实现是分叉的）。

    分叉的实际后果：容器给 4 核的 8 核机器上，去重按 os.cpu_count()=8 开 3 进程，
    而生成并发按 cpu_limit()=4 算 1~2 个任务 —— 两处互相不知道对方在抢核。
    """
    from app.db import milvus_store

    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_cgroup_v1_cpu_limit", lambda: None)
    monkeypatch.setattr(rg_module, "_affinity_count", lambda: 4)
    monkeypatch.setattr(rg_module, "_cpuset_count", lambda: None)

    assert rg_module.physical_cores() == 2
    assert milvus_store._physical_cores() == 2, (
        "milvus_store._physical_cores 与 resource_guard 口径不一致（去重与生成并发会互相不知道）"
    )


def test_health_brief_exposes_cpu_source():
    """health_brief 必须带出 cpu_source：回答"并发上限为什么是这个数"。"""
    b = rg_module.resource_guard.health_brief()
    assert "cpu_source" in b, "health_brief 缺少 cpu_source（无法判断是否读到容器 CPU 配额）"
    assert b["cpu_source"] in ("cgroup_v2", "cgroup_v1", "affinity", "cpuset", "cpu_count", "unknown")


# ============================================================================
# 物理核 vs 逻辑核口径（v4.1.5）
# ============================================================================

def _reset_topology_cache():
    """清掉进程内拓扑缓存——否则前一个用例探测到的真实值会污染后一个。"""
    with rg_module._TOPOLOGY_LOCK:
        rg_module._TOPOLOGY.clear()


def test_threads_per_core_from_real_cpuinfo(monkeypatch, tmp_path):
    """必须**读真实拓扑**（siblings / cpu cores），不能靠 ÷2 猜。

    夹具按本机真实形态（11th Gen i7-1165G7：1 socket × 4 核 × 2 线程 = 8 逻辑核），
    **必须写满 8 个 processor 段**——只写 2 段会与 siblings=8 自相矛盾
    （8 线程/socket 不可能只有 2 个逻辑处理器），会被交叉校验正确地拒绝。
    """
    _reset_topology_cache()
    p = tmp_path / "cpuinfo"
    lines = []
    for i in range(8):
        lines.append(f"processor\t: {i}\nphysical id\t: 0\nsiblings\t: 8\ncpu cores\t: 4\n")
    p.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(rg_module, "_read_text", lambda path: p.read_text(encoding="utf-8")
                        if "cpuinfo" in path else None)
    assert rg_module._threads_per_core() == 2
    _reset_topology_cache()


def test_threads_per_core_accepts_no_smt_machine(monkeypatch, tmp_path):
    """关超线程的真机（1 socket × 8 核 × 1 线程）必须采信 tpc=1，不能当成脏数据。

    这是"不能一概拒绝 tpc=1"的守卫：siblings=cores=8、8 个 processor、1 个 socket
    → 三条判据全过 → physical_cores(8)=8（并发预算翻倍是**正确**的，机器真有 8 核）。
    """
    _reset_topology_cache()
    p = tmp_path / "cpuinfo"
    lines = []
    for i in range(8):
        lines.append(f"processor\t: {i}\nphysical id\t: 0\nsiblings\t: 8\ncpu cores\t: 8\n")
    p.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(rg_module, "_read_text", lambda path: p.read_text(encoding="utf-8")
                        if "cpuinfo" in path else None)
    assert rg_module._threads_per_core() == 1
    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: 8.0)
    assert rg_module.physical_cores() == 8, "关超线程的 8 核机物理核就是 8，不是 4"
    _reset_topology_cache()


def test_threads_per_core_rejects_contradictory_proc_count(monkeypatch, tmp_path):
    """自相矛盾：siblings=8 但只有 2 个 processor → 必须拒绝并退回 ÷2。

    危害：若采信，tpc 算出 2、8 逻辑核 → 物理核 4；但这份 cpuinfo 本身不成立
    （8 线程/socket 不可能只暴露 2 个逻辑处理器），说明字段被填错，
    继续采信等于用脏数据定并发。
    """
    _reset_topology_cache()
    p = tmp_path / "cpuinfo"
    p.write_text(
        "processor\t: 0\nphysical id\t: 0\nsiblings\t: 8\ncpu cores\t: 4\n\n"
        "processor\t: 1\nphysical id\t: 0\nsiblings\t: 8\ncpu cores\t: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rg_module, "_read_text", lambda path: p.read_text(encoding="utf-8")
                        if "cpuinfo" in path else None)
    assert rg_module._threads_per_core() is None, "n_proc=2 与 siblings=8 矛盾，必须拒绝"
    _reset_topology_cache()


def test_physical_cores_uses_topology_not_divide_by_two(monkeypatch):
    """6 逻辑核 + 每核 2 线程 → 物理核 3（真测），而不是 `6÷2=3` 撞对的巧合。

    关键是**反例**：8 逻辑核 + 每核 1 线程（关超线程）时 ÷2 会错报 4，
    真实物理核是 8。这一条专门锁住"不能再用 ÷2"。
    """
    _reset_topology_cache()
    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: 8.0)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 1)
    assert rg_module.physical_cores() == 8, (
        "关超线程的机器上物理核应等于逻辑核；÷2 会误报 4，把并发预算砍半"
    )
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    assert rg_module.physical_cores() == 4
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 4)  # 假想 4 路 SMT
    assert rg_module.physical_cores() == 2
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 3)
    assert rg_module.physical_cores() == 3, "8/3 应向上取整为 3（ceil），不能 floor 成 2"
    _reset_topology_cache()


def test_physical_cores_falls_back_to_half_when_topology_unreadable(monkeypatch):
    """拓扑读不到时必须退回 ÷2 保守估计（宁少不多），且绝不为 0。"""
    _reset_topology_cache()
    monkeypatch.setattr(rg_module, "_cgroup_v2_cpu_limit", lambda: 6.0)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: None)
    assert rg_module.physical_cores() == 3
    assert rg_module._topology_source() == "heuristic"
    _reset_topology_cache()


def test_topology_source_reports_cpuinfo_when_available(monkeypatch):
    """cpu_topology 字段必须能区分"测出来的"与"猜出来的"——部署排查靠它。"""
    _reset_topology_cache()
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    assert rg_module._topology_source() == "cpuinfo"
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: None)
    assert rg_module._topology_source() == "heuristic"
    _reset_topology_cache()


def test_encode_threads_capped_by_both_best_and_logical(monkeypatch):
    """单次 encode 线程数 = min(实测最优 4, 物理核, 逻辑核)，且至少 1。

    这是"并发按物理核、单次线程按逻辑核封顶"两条口径的交汇点：
    2 核容器若仍开 4 线程就是自伤（超配只会增加切换开销）。
    """
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 8)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 16.0)
    assert rg_module.encode_threads() == 4  # 实测最优封顶

    monkeypatch.setattr(rg_module, "physical_cores", lambda: 2)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 2.0)
    assert rg_module.encode_threads() == 2  # 小机器收缩

    monkeypatch.setattr(rg_module, "physical_cores", lambda: 1)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 1.0)
    assert rg_module.encode_threads() == 1  # 绝不为 0


def test_task_ceiling_derates_against_logical_cores_not_physical(monkeypatch):
    """负载降级的比较基准必须是**逻辑核**（与 load_1m 同量纲）。

    反例固定住真实缺陷：8 逻辑核 / 4 物理核的机器上，load_1m=5.0 属于正常
    负载（8 个可运行槽位里占 5 个），但对比物理核 4 会被误判"过载"而降级。
    """
    monkeypatch.setattr(rg_module, "inference_slots", lambda: 3)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    snap = {"cpu_percent_avg": 95.0, "load_1m": 5.0, "memory": {"available_ratio": 0.5}}
    assert task_ceiling(snap) == 3, (
        "load_1m=5.0 在 8 逻辑核机器上不算过载，不能因为物理核是 4 就降级"
    )
    # 真的过载（load 超过逻辑核数）才降级
    snap["load_1m"] = 9.0
    assert task_ceiling(snap) == 2


def test_threads_per_core_rejects_non_smt_ratio(monkeypatch, tmp_path):
    """非现实 SMT 比值（如 3、5、6）必须拒绝——现实中只有 1/2/4/8。"""
    _reset_topology_cache()
    p = tmp_path / "cpuinfo"
    # siblings=12 / cores=4 → tpc=3，不是现实 SMT 比值
    p.write_text(
        "processor\t: 0\nsiblings\t: 12\ncpu cores\t: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rg_module, "_read_text", lambda path: p.read_text(encoding="utf-8")
                        if "cpuinfo" in path else None)
    assert rg_module._threads_per_core() is None
    _reset_topology_cache()


def test_threads_per_core_accepts_normal_smt(monkeypatch, tmp_path):
    """正常 2 路 SMT（siblings=8 / cores=4 / 8 个 processor）必须采信。"""
    _reset_topology_cache()
    p = tmp_path / "cpuinfo"
    lines = []
    for i in range(8):
        lines.append(f"processor\t: {i}\nphysical id\t: 0\nsiblings\t: 8\ncpu cores\t: 4\n")
    p.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(rg_module, "_read_text", lambda path: p.read_text(encoding="utf-8")
                        if "cpuinfo" in path else None)
    assert rg_module._threads_per_core() == 2
    _reset_topology_cache()


def test_apply_torch_threads_sets_intra_op_without_torch(monkeypatch):
    """apply_torch_threads 在无 torch 时必须静默跳过，不能阻断启动。

    且返回值必须等于"本应设置的值"——调用方/单测据此判断口径。
    """
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    got = rg_module.apply_torch_threads()
    assert got == rg_module.encode_threads(), "无 torch 时返回值仍应是本应设置的值"
    assert got >= 1


def test_milvus_store_delegates_to_resource_guard(monkeypatch):
    """milvus_store 必须**委托** resource_guard 算物理核，不允许再有一份实现。"""
    from app.db import milvus_store

    monkeypatch.setattr(rg_module, "physical_cores", lambda: 5)
    assert milvus_store._physical_cores() == 5, (
        "milvus_store 又自己算了一遍物理核（两份实现必然漂移，历史已证明）"
    )


def test_stats_and_health_expose_topology_fields():
    """stats/health_brief 都要带出拓扑来源与 encode 线程数，前端据此解释预算。"""
    s = rg_module.resource_guard.stats()
    assert s["cpu"]["topology_source"] in ("cpuinfo", "heuristic")
    assert s["cpu"]["encode_threads"] >= 1
    assert "threads_per_core" in s["cpu"]
    b = rg_module.resource_guard.health_brief()
    assert b["cpu_topology"] in ("cpuinfo", "heuristic")
    assert b["encode_threads"] >= 1


# ============================================================================
# 跨容器预算投影（v4.1.5）：api 与 celery 是两个容器，各算各的会超卖
# ============================================================================

@pytest.fixture(autouse=True)
def _isolate_peer_budget(monkeypatch, tmp_path):
    """每条用例前后都清空投影通道（**包括环境变量**）。

    为什么必须 autouse：容器里跑测试时，`PEER_CELERY_CPU` 由 compose 注入了
    主进程环境，`docker exec` 起的 pytest **会继承它** → 所有依赖"无投影"的
    断言全部失真（实测：7 条无关用例集体变红）。
    这与 `CELERY_WORKER_PROCESS` 那次是同一类污染，但那次的选择是"不用环境变量
    传标志"，而投影**必须**用环境变量（跨进程可见性要求），所以这里改为
    测试侧强制隔离。

    ⚠️ 只 `monkeypatch.delenv` 不够（实测无效）：容器启动注入的变量在某些
    环境下会被重新写回 os.environ。因此这里**额外把 `_PEER_ENV_KEYS` 换成空元组**，
    从读取入口上彻底屏蔽 env 通道 —— 需要 env 通道的用例自己 setenv 后再
    monkeypatch.setattr 回真实 keys。

    ⚠️ 还要屏蔽**落盘通道**（第二次踩）：部署跑过之后容器里真的存在
    `/app/data/.peer_cpu_budget`，测试会老老实实读它 → 用例里"无投影"的前提不成立。
    把候选路径指向 tmp 下一个不存在的位置（不要直接置空元组，否则测不到真实路径逻辑）。
    """
    rg_module._PEER_FILE_CACHE.clear()
    for k in rg_module._PEER_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rg_module, "_PEER_ENV_KEYS", ())
    monkeypatch.setattr(rg_module, "_PEER_FILE_CANDIDATES", (str(tmp_path / ".peer_cpu_budget"),))
    yield
    rg_module._PEER_FILE_CACHE.clear()


def _enable_env_channel(monkeypatch, keys=("PEER_CELERY_CPU", "PEER_CELERY_CPU_CORES")):
    """恢复 env 通道（autouse fixture 默认把它屏蔽了）。"""
    monkeypatch.setattr(rg_module, "_PEER_ENV_KEYS", tuple(keys))


def _clear_peer(monkeypatch=None):
    """清空投影（三条通道都要清，否则测试之间互相污染）。

    为什么不能只 clear 一个 dict：v4.1.5 实测发现 `docker exec` 起的进程
    写进程内 dict 时 uvicorn 主进程读不到 → 投影改走「环境变量 + 落盘文件」，
    并加了 `_PEER_FILE_CACHE` 做一次性缓存。三者任一残留都会让断言失真。
    """
    rg_module._PEER_FILE_CACHE.clear()
    if monkeypatch is not None:
        for k in rg_module._PEER_ENV_KEYS:
            monkeypatch.delenv(k, raising=False)


def test_peer_budget_none_is_noop():
    """未投影时 effective_cpu_limit 必须等于自身配额（不改旧行为）。"""
    _clear_peer()
    own = rg_module.cpu_limit()
    assert abs(rg_module.effective_cpu_limit() - max(1.0, float(own))) < 1e-6


def test_peer_budget_is_subtracted_not_capped(monkeypatch):
    """语义必须是**减法**（自身 − 兄弟占用），不是取 min。

    反例（8 核机）：api 限 8、celery 占 2。
      · 取 min → 2（错：api 明明还有 6 核可用，却被压成 celery 的额度）
      · 减法   → 6（对）
    """
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)

    rg_module.set_peer_cpu_budget(2, source="test")
    assert rg_module.effective_cpu_limit() == 6.0, "兄弟占用必须做减法，不能当成上限"

    # 兄弟占用超过自身 → 仍保底 1（绝不返回 0，否则生成永远进不来）
    rg_module.set_peer_cpu_budget(20, source="test")
    assert rg_module.effective_cpu_limit() == 1.0

    _clear_peer(monkeypatch)


def test_peer_budget_cleared_by_none_or_zero(monkeypatch):
    """传 None / 0 必须撤销投影（撤销能力是幂等部署的前提）。"""
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 6.0)
    rg_module.set_peer_cpu_budget(3)
    assert rg_module.effective_cpu_limit() == 3.0
    rg_module.set_peer_cpu_budget(None)
    assert rg_module.effective_cpu_limit() == 6.0
    rg_module.set_peer_cpu_budget(3)
    rg_module.set_peer_cpu_budget(0)
    assert rg_module.effective_cpu_limit() == 6.0
    _clear_peer(monkeypatch)


def test_peer_env_always_returns_tuple(monkeypatch):
    """env 通道命中时必须显式 return，不能隐式返回 None。

    v4.1.6 实测事故：给 `_read_peer_cpu_budget` 加"落盘只读一次"的分叉时，
    把 env 分支的解析整段包进了 `if not raw:`，于是 env 命中后函数走到末尾
    隐式返回 None → 调用方 `[0]` 抛 `TypeError: 'NoneType' object is not
    subscriptable` → **`/api/health` 直接 500**（比"投影不生效"严重得多）。
    类型标注是 tuple 不代表运行时会兜底，所以这里对**所有取值**都断言类型。
    """
    _clear_peer(monkeypatch)
    _enable_env_channel(monkeypatch)
    for val in ("3", "0", "-1", "abc", "  "):
        monkeypatch.setenv("PEER_CELERY_CPU", val)
        got = rg_module._read_peer_cpu_budget()
        assert isinstance(got, tuple) and len(got) == 2, f"env={val!r} 时返回了 {got!r}"
        assert isinstance(got[0], float) and isinstance(got[1], str)
    _clear_peer(monkeypatch)


def test_health_brief_survives_peer_env(monkeypatch):
    """/api/health 的探活口径：有投影时 health_brief 不能抛异常。"""
    _clear_peer(monkeypatch)
    _enable_env_channel(monkeypatch)
    monkeypatch.setenv("PEER_CELERY_CPU", "3")
    b = rg_module.resource_guard.health_brief()
    assert b["cpu_peer_budget"] == 3.0
    assert b["cpu_peer_budget_source"].startswith("env:")
    _clear_peer(monkeypatch)


def test_peer_budget_env_channel_wins(monkeypatch):
    """容器重建时 compose 注入的**环境变量**必须优先于落盘文件。

    为什么这条重要：环境变量是"这次部署真正生效的声明"，落盘文件是历史残留。
    若文件赢，就会出现「override 已改、容器也重建了，预算还按上次的算」。
    """
    _clear_peer(monkeypatch)
    _enable_env_channel(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    rg_module.set_peer_cpu_budget(5, source="file-stale")   # 历史残留
    monkeypatch.setenv("PEER_CELERY_CPU", "2")
    val, src = rg_module._read_peer_cpu_budget()
    assert val == 2.0 and src.startswith("env:"), "环境变量必须赢过落盘文件"
    assert rg_module.effective_cpu_limit() == 6.0
    _clear_peer(monkeypatch)


def test_peer_file_is_read_once_and_cached(monkeypatch, tmp_path):
    """落盘通道只读一次：`health_brief()` 被 /api/health 每次探活调用。

    若每次都 open()，就是**同步 IO 进事件循环**——本项目已因同类问题吃过亏
    （bge embedding 同步实现漏包 asyncio.to_thread 导致生成期全站超时）。
    验证：先读一次建立缓存，之后把文件改成另一个值，读到的必须还是旧值。
    """
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    f = tmp_path / ".peer_cpu_budget"
    f.write_text("3\n# source=test\n", encoding="utf-8")
    monkeypatch.setattr(rg_module, "_PEER_FILE_CANDIDATES", (str(f),))

    val1, src1 = rg_module._read_peer_cpu_budget()
    assert val1 == 3.0 and src1 == f"file:{f}"

    f.write_text("7\n", encoding="utf-8")   # 文件被改
    val2, _src2 = rg_module._read_peer_cpu_budget()
    assert val2 == 3.0, "落盘投影必须只读一次（否则每次探活都做同步文件 IO）"

    # 清缓存后才能读到新值（等价于"重启容器"）
    rg_module._PEER_FILE_CACHE.clear()
    assert rg_module._read_peer_cpu_budget()[0] == 7.0
    _clear_peer(monkeypatch)


def test_peer_file_absent_result_is_cached_too(monkeypatch, tmp_path):
    """文件不存在也要缓存——否则每次探活都去 stat 两个候选路径。"""
    _clear_peer(monkeypatch)
    monkeypatch.setattr(
        rg_module, "_PEER_FILE_CANDIDATES", (str(tmp_path / "nope1"), str(tmp_path / "nope2"))
    )
    assert rg_module._read_peer_cpu_budget() == (0.0, "")
    assert rg_module._PEER_FILE_CACHE, "未命中也要落缓存，避免反复 stat"


def test_peer_file_garbage_value_is_ignored(monkeypatch, tmp_path):
    """落盘内容不是数字时按"未投影"处理，不能抛异常拖垮探活。"""
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 4.0)
    f = tmp_path / ".peer_cpu_budget"
    f.write_text("abc\n", encoding="utf-8")
    monkeypatch.setattr(rg_module, "_PEER_FILE_CANDIDATES", (str(f),))
    assert rg_module._read_peer_cpu_budget() == (0.0, "")
    assert rg_module.effective_cpu_limit() == 4.0
    _clear_peer(monkeypatch)


def test_task_ceiling_shrinks_when_peer_declared(monkeypatch):
    """小机器上 api+celery 合计不能超过整机物理核。

    夹具：逻辑核 4 / 每核 2 线程 → 物理核 2；无投影时 ceiling=min(slots=1,…)=1。
    兄弟占 3 核 → 可用逻辑核 4−3=1 → 物理核 1 → ceiling 必须仍是 1（不是 0）。
    """
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 4.0)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 2)
    monkeypatch.setattr(rg_module, "_cfg", lambda n, d: 4 if n == "GEN_TASK_CONCURRENCY_MAX" else d)

    snap = {"cpu_percent_avg": 0.0, "load_1m": 0.0, "memory": {"available_ratio": 0.9}}
    before = rg_module.task_ceiling(snap)
    rg_module.set_peer_cpu_budget(3.0, source="test")
    after = rg_module.task_ceiling(snap)
    _clear_peer(monkeypatch)
    assert before == 1
    assert after == 1, "投影后 ceiling 不得放大，也不得降到 0（生成会永远进不来）"


def test_task_ceiling_peer_deduction_reduces_ceiling_visibly(monkeypatch):
    """能明显区分的一档：物理 4 核 → slots 3。

    兄弟占 4 逻辑核 = floor(4/2) = **2 个整物理核** → 可用 4−2=2 → ceiling=2。

    ⚠️ v4.1.7 起期望值是 2 而非 1：旧实现减完还多 `-1`（与 slots 内部的
    "留 1 核"重复），属于同一理由扣两遍。详见
    `test_task_ceiling_peer_deduction_units_are_consistent`。
    """
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "_cfg", lambda n, d: 4 if n == "GEN_TASK_CONCURRENCY_MAX" else d)
    snap = {"cpu_percent_avg": 0.0, "load_1m": 0.0, "memory": {"available_ratio": 0.9}}

    assert rg_module.task_ceiling(snap) == 3
    rg_module.set_peer_cpu_budget(4.0, source="test")
    assert rg_module.task_ceiling(snap) == 2
    _clear_peer(monkeypatch)


def test_task_ceiling_peer_small_overhead_stays_gentle(monkeypatch):
    """兄弟只占少量核时不能过度惩罚：物理 4 核，兄弟占 2 逻辑核。

    2 逻辑核 = floor(2/2) = 1 个整物理核 → 可用 3 → slots 已是 3，ceiling 不变。
    （原期望 2 是因为旧实现把 1.5 个物理核向上取整成 2 再减，多算了半个核。）
    """
    _clear_peer(monkeypatch)
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "_cfg", lambda n, d: 4 if n == "GEN_TASK_CONCURRENCY_MAX" else d)
    snap = {"cpu_percent_avg": 0.0, "load_1m": 0.0, "memory": {"available_ratio": 0.9}}

    assert rg_module.task_ceiling(snap) == 3
    rg_module.set_peer_cpu_budget(2.0, source="test")
    assert rg_module.task_ceiling(snap) == 3, "兄弟只占 1 个整物理核，api 仍应保有 3 个并发"
    _clear_peer(monkeypatch)


def test_health_brief_exposes_peer_budget():
    """投影必须可见：现场要能回答"预算为什么被压小了"。"""
    b = rg_module.resource_guard.health_brief()
    assert "cpu_peer_budget" in b
    assert "cpu_effective_logical_cores" in b
    s = rg_module.resource_guard.stats()
    assert "peer_cpu_budget" in s["cpu"]
    assert "effective_logical_cores" in s["cpu"]


def test_cpu_overload_happens_before_mem_oom(monkeypatch):
    """CPU 打满 + 内存告急**同时**成立时，两条保护都必须执行到。

    旧实现先判内存并 `return 1`，于是"CPU 也打满了"这条信息永远不体现在结果里。
    现在顺序改为：CPU 先降一档（不 return）→ 内存再压到 1。
    验证方式：构造"CPU 打满 + 内存**正常**"→ 结果应为 base-1；再加内存告急 → 1。
    两条都成立时才为 1，说明 CPU 分支没有被内存分支挡住（反之亦然）。
    """
    monkeypatch.setattr(rg_module, "_PEER_FILE_CACHE", {})
    monkeypatch.setattr(rg_module, "cpu_limit", lambda: 8.0)
    monkeypatch.setattr(rg_module, "physical_cores", lambda: 4)
    monkeypatch.setattr(rg_module, "_threads_per_core", lambda: 2)
    monkeypatch.setattr(
        rg_module, "_cfg", lambda n, d: 4 if n == "GEN_TASK_CONCURRENCY_MAX" else d
    )

    cpu_hot = {"cpu_percent_avg": 95.0, "load_1m": 20.0, "memory": {"available_ratio": 0.9}}
    # 基础 3（物理核 4 → slots 3）→ CPU 打满降一档 = 2
    assert rg_module.task_ceiling(cpu_hot) == 2, "CPU 降级应只降一档（不 return）"

    both_hot = dict(cpu_hot)
    both_hot["memory"] = {"available_ratio": 0.05}
    assert rg_module.task_ceiling(both_hot) == 1, "内存告急必须压到 1"


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
