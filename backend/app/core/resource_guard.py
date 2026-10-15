"""服务器资源感知与生成并发控制（v4.0.2）。

## 要解决的问题

S1 根治后残留一类**容量层面**的现象：并发生成时事件循环仍有 0.5-2.2s 的 warn 级
卡顿。看门狗取证显示挂起协程停在 `_gen_sem` 信号量与 `gen_agent.generate`
——即**多个 FP 同时做本地模型推理（bge embedding / CrossEncoder 重排）时的
CPU/GIL 争抢**，不是某个单点阻塞。

本系统是**平台**：多人可能同时点「生成」。此前只有两道闸：

    ① `task_guard.reserve_generation_slot`：同一 req_id 互斥（防同需求双跑）
    ② orchestrator 内 `_gen_sem`：单任务内 FP 并发度（kb_settings 可调，1-5）

两道都**不看服务器还能吃多少**。N 个用户同时生成 = N × FP并发 个本地推理
同时开跑，8 核机器上实测会把事件循环拖到秒级。用户原话：
「根据服务器的 cpu 大小进行限制」「资源不足可以等待资源释放」。

## 三道机制

1. **资源探测**（纯标准库，不引 psutil）——全部在**后台采样线程**里做：
   - CPU 上限：cgroup v2 `cpu.max` → cgroup v1 quota/period → `sched_getaffinity`
     → `cpu_count`。**容器配额优先**——`nproc` 在容器里看到的是宿主逻辑核，
     不一定是自己真能用的（本项目 compose 未设 cpus，故退化为 8）。
   - 物理核：逻辑核 ÷ 2 去超线程（与 `db/milvus_store._physical_cores` 同口径）。
   - 内存：cgroup `memory.max/current` → `/proc/meminfo`。容器有上限时
     **以 cgroup 为准**——那才是真正的 OOM 线（宿主 MemTotal 会把"容器快饿死"
     误判成"内存充足"）。
   - CPU 使用率：`/proc/stat` 差值 + 10s 窗口平均（瞬时值对排程毫无意义）。

   ⚠️ 为什么必须放后台线程：读 /proc 是**同步 IO**，S1 立下的纪律就是"同步 IO
   不进事件循环"。而且并发预算要在等待循环里**每秒重判**，若每次都现读会把
   探测本身变成阻塞源。故采样线程 2s 刷新一次缓存，预算判定只读内存。

2. **并发预算**（`inference_slots`）：本地推理能占的"核份额"。
   `max(1, 物理核 - 1)`——留 1 核给主进程/事件循环，与
   `milvus_store._dedup_workers_configured` 的 `auto = 物理核-1` 完全同口径。
   - 全局生成任务上限 `task_ceiling()` = min(inference_slots, 配置硬上限)，
     再按**运行时负载降级**（CPU 打满或内存紧张时收紧）。
   - 每任务 FP 并发 `clamp_fp_concurrency(cfg)` = min(cfg, slots // 活跃任务数)。
     → **单任务时与今天完全一致**（无回归），多任务时自动平分预算。

3. **准入门 + 等待队列**（`GenerationGate`）：资源不足时**排队等待释放**
   （不拒绝），但有双上限防无限等：等待者数上限 + 等待时长上限。

## 设计纪律

- **fail-open**：探测失败一律退保守默认值，绝不让监控/探测失败阻断生成。
- **可观测**：`stats()` 输出全部推导量，首页仪表盘直接展示——
  "CPU 8 核 / 推理份额 3 / 当前 2 任务并发 / 排队 1"，用户能自己看懂为什么在等。
- **release 必须同步**：见 `GenerationGate` docstring——SSE 生成器的 finally
  在客户端断连时处于已取消作用域，async release 会永远执行不到。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from app.utils.logger import logger

# ============================================================================
# 配置（都有 settings 兜底，缺失时用默认值）
# ============================================================================

#: 全局并发生成任务的**硬上限**（防误配打爆供应商/机器）
GEN_TASK_CONCURRENCY_MAX = 4
#: 运行时 CPU 使用率达到该百分比且负载超过物理核 → 预算降级一档
CPU_OVERLOAD_PERCENT = 85.0
#: 可用内存占比低于该值 → 预算压到 1（防 OOM）
MEM_LOW_RATIO = 0.15
#: 排队等待上限（秒）：超过则返回"系统繁忙"让用户稍后再试
GEN_RESOURCE_WAIT_TIMEOUT_S = 300.0
#: 同时排队者上限（超过直接拒绝，防无限堆积）
GEN_RESOURCE_MAX_WAITERS = 8
#: 资源采样间隔（秒）
_SAMPLE_INTERVAL = 2.0
#: CPU 使用率的窗口平均长度（样本数，× 间隔 ≈ 10s）
_CPU_AVG_WINDOW = 5


def _cfg(name: str, default: Any) -> Any:
    """读 settings，失败/缺失都返回默认值（fail-open）。"""
    try:
        from app.config import settings

        v = getattr(settings, name, None)
        return default if v is None else v
    except Exception:  # noqa: BLE001 - 配置不可用不能阻断资源守卫
        return default


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:  # noqa: BLE001 - 文件不存在/无权限都走下一个探测源
        return None


# ============================================================================
# 静态资源探测（进程启动即可确定，不随时间变）
# ============================================================================

def _cgroup_v2_cpu_limit() -> float | None:
    """cgroup v2：`/sys/fs/cgroup/cpu.max` = `<quota> <period>`，quota 可为 `max`。"""
    raw = _read_text("/sys/fs/cgroup/cpu.max")
    if not raw:
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    quota, period = parts[0], parts[1]
    if quota == "max":
        return None  # 无配额限制
    try:
        q, p = float(quota), float(period)
        if q <= 0 or p <= 0:
            return None
        return q / p
    except ValueError:
        return None


def _cgroup_v1_cpu_limit() -> float | None:
    """cgroup v1：quota/period 两个文件，quota 为 -1 表示无限制。"""
    q_raw = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    p_raw = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if not q_raw or not p_raw:
        return None
    try:
        q, p = float(q_raw), float(p_raw)
        if q <= 0 or p <= 0:
            return None
        return q / p
    except ValueError:
        return None


def _affinity_count() -> int | None:
    """Linux 可用核数（考虑 cpuset/taskset 收缩）；非 Linux 平台无此 API。"""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None


def cpu_limit() -> float:
    """本进程真正能用的 CPU 核数（浮点，容器配额可能是 2.5 这种）。

    优先级：cgroup 配额 → affinity → cpu_count。
    ⚠️ 不能只用 `os.cpu_count()`：容器里它返回**宿主**逻辑核，
       而 cgroup 配额可能只给了 2 核——按 8 核定并发会把机器打穿。
    """
    for probe in (_cgroup_v2_cpu_limit, _cgroup_v1_cpu_limit):
        try:
            v = probe()
        except Exception:  # noqa: BLE001
            v = None
        if v and v > 0:
            return round(v, 2)
    aff = _affinity_count()
    if aff and aff > 0:
        return float(aff)
    return float(os.cpu_count() or 1)


def physical_cores() -> int:
    """保守估物理核（逻辑核 ÷ 2 去超线程；容器内 cpu_count 含超线程）。

    与 `db/milvus_store._physical_cores` 同口径——那边靠它决定去重进程池
    worker 数，这边靠它决定生成并发。**两处必须同口径**，否则"去重开 3 个
    进程 × 4 线程"与"同时跑 4 个生成任务"会互相不知道对方在抢核。
    """
    try:
        return max(1, (int(cpu_limit()) or 2) // 2)
    except Exception:  # noqa: BLE001
        return 1


def inference_slots() -> int:
    """本地模型推理可用的"核份额"（留 1 核给主进程/事件循环）。"""
    return max(1, physical_cores() - 1)


def _mem_from_cgroup() -> tuple[int, int] | None:
    """cgroup 内存（total, available）字节数；无上限时返回 None。"""
    mx = _read_text("/sys/fs/cgroup/memory.max")
    cur = _read_text("/sys/fs/cgroup/memory.current")
    if mx and mx != "max" and cur:
        try:
            total_b, used_b = int(mx), int(cur)
            if total_b > 0:
                return total_b, max(0, total_b - used_b)
        except ValueError:
            pass
    return None


def _mem_from_proc() -> tuple[int, int] | None:
    """/proc/meminfo 的 (MemTotal, MemAvailable) 字节数。"""
    info = _read_text("/proc/meminfo")
    if not info:
        return None
    vals: dict[str, int] = {}
    for line in info.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        try:
            vals[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024
        except (ValueError, IndexError):
            continue
    total = vals.get("MemTotal")
    if not total:
        return None
    return total, vals.get("MemAvailable", vals.get("MemFree", 0))


def _mem_snapshot_raw() -> dict:
    """内存快照（同步，只允许在采样线程里调）。"""
    cg = _mem_from_cgroup()
    if cg is not None:
        total, avail = cg
        source = "cgroup"
    else:
        pr = _mem_from_proc()
        if pr is None:
            return {"total_mb": 0, "available_mb": 0, "available_ratio": None, "source": "unknown"}
        total, avail = pr
        source = "proc"
    return {
        "total_mb": round(total / 1048576),
        "available_mb": round(avail / 1048576),
        "available_ratio": round(avail / total, 3) if total > 0 else None,
        "source": source,
    }


def _cpu_times() -> tuple[int, int] | None:
    """/proc/stat 首行的 (总时间, 空闲时间)；idle 含 iowait。"""
    raw = _read_text("/proc/stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("cpu "):
            try:
                vals = [int(x) for x in line.split()[1:]]
            except ValueError:
                return None
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
    return None


# ============================================================================
# 后台采样线程
# ============================================================================

class _ResourceSampler(threading.Thread):
    """后台线程：周期性刷新 CPU 使用率 / 负载 / 内存快照。

    所有 /proc 读取都在这里完成——**事件循环只读缓存**。这既是 S1 的纪律
    （同步 IO 不进 async 上下文），也是因为并发预算要在等待循环里频繁重判，
    现读会把探测本身变成阻塞源。
    """

    def __init__(self, interval: float = _SAMPLE_INTERVAL) -> None:
        super().__init__(name="resource-sampler", daemon=True)
        self._interval = interval
        self._stopped = False
        self._lock = threading.Lock()
        self._snap: dict[str, Any] = {
            "cpu_percent": 0.0,
            "cpu_percent_avg": 0.0,
            "load_1m": 0.0, "load_5m": 0.0, "load_15m": 0.0,
            "memory": {"total_mb": 0, "available_mb": 0, "available_ratio": None, "source": "unknown"},
            "sampled_at": "",
            "error": "",
        }
        self._window: list[float] = []

    def run(self) -> None:
        while not self._stopped:
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 - 采样失败绝不影响业务
                with self._lock:
                    self._snap["error"] = str(e)[:120]
            if self._stopped:
                break
            time.sleep(self._interval)

    def _tick(self) -> None:
        a = _cpu_times()
        # 采样窗口就是睡眠窗口：睡眠中 /proc/stat 的计数器自然累积出差值
        end = time.monotonic() + self._interval
        while not self._stopped:
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, 0.2))
        b = _cpu_times()
        pct = 0.0
        if a and b and b[0] > a[0]:
            pct = max(0.0, min(100.0, (b[0] - a[0] - (b[1] - a[1])) / (b[0] - a[0]) * 100.0))
        self._window.append(pct)
        if len(self._window) > _CPU_AVG_WINDOW:
            self._window.pop(0)

        try:
            load = os.getloadavg()
        except (AttributeError, OSError):
            load = (0.0, 0.0, 0.0)

        with self._lock:
            self._snap["cpu_percent"] = round(pct, 1)
            self._snap["cpu_percent_avg"] = round(sum(self._window) / len(self._window), 1)
            self._snap["load_1m"], self._snap["load_5m"], self._snap["load_15m"] = (
                round(load[0], 2), round(load[1], 2), round(load[2], 2),
            )
            self._snap["memory"] = _mem_snapshot_raw()
            self._snap["sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._snap["error"] = ""

    def stop(self) -> None:
        self._stopped = True

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)


# ============================================================================
# 并发预算（只读缓存，可在事件循环里安全调用）
# ============================================================================

def task_ceiling(snap: dict | None = None) -> int:
    """当前允许的**全局并发生成任务数**上限。

    基础 = min(推理份额, 配置硬上限)；再按运行时负载降级：
      - CPU 窗口均值 ≥ 85% 且 1 分钟负载 > 物理核 → 收紧一档（机器已经在喘）
      - 可用内存 < 15% → 压到 1（防 OOM Kill，那比慢更致命）

    Args:
        snap: 采样快照；None 时现场取（采样线程未启动时的兜底）
    """
    base = min(inference_slots(), int(_cfg("GEN_TASK_CONCURRENCY_MAX", GEN_TASK_CONCURRENCY_MAX)))
    base = max(1, int(base))
    try:
        if snap is None:
            snap = _sampler.snapshot()
        mem = snap.get("memory") or {}
        ratio = mem.get("available_ratio")
        if ratio is not None and ratio < float(_cfg("MEM_LOW_RATIO", MEM_LOW_RATIO)):
            return 1
        if (snap.get("cpu_percent_avg") or 0) >= float(_cfg("CPU_OVERLOAD_PERCENT", CPU_OVERLOAD_PERCENT)) \
                and (snap.get("load_1m") or 0) > physical_cores():
            base = max(1, base - 1)
    except Exception:  # noqa: BLE001 - 探测失败保持基础值（fail-open）
        pass
    return base


# ============================================================================
# 准入门（等待队列）
# ============================================================================

class GenerationGate:
    """全局生成任务准入门：限额准入 + 资源不足时排队等待释放。

    为什么不用 `asyncio.Semaphore`：限额是**动态**的（随 CPU 负载与活跃任务数
    变），而 Semaphore 的 value 不能在运行中安全调整——改它要么丢失已排队的
    等待者，要么造成超额放行。这里每次唤醒都重判限额，语义正确。

    为什么不用 `asyncio.Condition`（本类第一版）：`Condition.notify_all()` 必须
    持锁，导致 `release()` 只能是 async。而 `generate.py` 的释放点在 SSE 生成器
    的 `finally` 里——**客户端断连时该作用域已被取消，任何 await 都会立刻再抛
    CancelledError，release 根本执行不到**，槽位永久泄漏、后续任务全部堵死
    （同文件 `_shielded_reaper` 的存在正是为绕开同一个坑）。

    故最终形态：**计数器用 `threading.Lock`（同步），唤醒用 `asyncio.Event`**。
    `release()` 因此是**纯同步**的，取消作用域里也能安全执行。
    等待侧用「先 clear → 再判据 → 后 wait」的顺序规避丢失唤醒；并以
    `POLL_S` 超时兜底（Event 万一失效也只是多等一拍，不会死等）。
    """

    #: 等待轮询的超时上限（秒）——既是唤醒失效的兜底，也限制断连检测延迟
    POLL_S = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: list[str] = []
        self._waiting: list[str] = []
        self._wakeup: asyncio.Event | None = None
        self._wakeup_loop: asyncio.AbstractEventLoop | None = None
        self._admitted_total = 0
        self._rejected_total = 0
        self._max_wait_ms = 0.0
        self._last_wait_ms = 0.0
        self._last_reject_reason = ""

    # ---------------------------------------------------------------- 内部

    def _event(self) -> asyncio.Event:
        """按当前运行循环取/建唤醒 Event。

        uvicorn `--reload` 会在同一进程内换事件循环；跨 loop 复用 Event 会
        RuntimeError。故按 loop 身份重建（计数器是 threading.Lock 保护的普通
        list，不受换 loop 影响）。
        """
        loop = asyncio.get_running_loop()
        if self._wakeup is None or self._wakeup_loop is not loop:
            self._wakeup = asyncio.Event()
            self._wakeup_loop = loop
        return self._wakeup

    def _wake(self) -> None:
        """唤醒所有等待者（同步；异常只影响"早醒一拍"，不会死等）。"""
        ev = self._wakeup
        if ev is None:
            return
        try:
            ev.set()
        except Exception as e:  # noqa: BLE001 - 唤醒失败由 POLL_S 超时兜底
            logger.debug("[ResourceGate] 唤醒等待者失败（由轮询兜底）: {}", str(e)[:100])

    def _try_take(self, task_id: str) -> bool:
        """在锁内尝试占位（重判动态限额）。"""
        with self._lock:
            if len(self._active) < task_ceiling(_sampler.snapshot()):
                self._active.append(task_id)
                self._admitted_total += 1
                return True
            return False

    # ---------------------------------------------------------------- 查询

    @property
    def active_tasks(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active": len(self._active),
                "waiting": len(self._waiting),
                "ceiling": task_ceiling(_sampler.snapshot()),
                "active_task_ids": list(self._active),
                "admitted_total": self._admitted_total,
                "rejected_total": self._rejected_total,
                "max_wait_ms": round(self._max_wait_ms, 1),
                "last_wait_ms": round(self._last_wait_ms, 1),
                "last_reject_reason": self._last_reject_reason,
            }

    # ---------------------------------------------------------------- 准入

    async def acquire(
        self,
        task_id: str,
        *,
        max_wait_s: float | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict:
        """申请一个生成槽位；资源不足时排队等待释放。

        Args:
            task_id: 任务 ID（用于观测与释放）
            max_wait_s: 最长等待秒数，None 用配置默认（300s）
            abort_check: 可选，等待期间轮询（客户端断连等）；返回 True 立即放弃

        Returns:
            {"admitted": bool, "waited_ms": float, "reason": str, ...}
        """
        timeout = float(_cfg("GEN_RESOURCE_WAIT_TIMEOUT_S", GEN_RESOURCE_WAIT_TIMEOUT_S)) \
            if max_wait_s is None else float(max_wait_s)
        max_waiters = int(_cfg("GEN_RESOURCE_MAX_WAITERS", GEN_RESOURCE_MAX_WAITERS))
        started = time.monotonic()

        # 快路径：有空位直接进（绝大多数请求走这里，零等待开销）
        if self._try_take(task_id):
            return {
                "admitted": True, "waited_ms": 0.0, "reason": "",
                "ceiling": task_ceiling(_sampler.snapshot()), "queue_position": 0,
            }
        with self._lock:
            if len(self._waiting) >= max_waiters:
                self._rejected_total += 1
                self._last_reject_reason = "queue_full"
                return {
                    "admitted": False, "waited_ms": 0.0, "reason": "queue_full",
                    "ceiling": task_ceiling(_sampler.snapshot()),
                    "queue_position": len(self._waiting) + 1,
                }
            self._waiting.append(task_id)

        def _reject(reason: str) -> dict:
            self._rejected_total += 1
            self._last_reject_reason = reason
            return {
                "admitted": False,
                "waited_ms": round((time.monotonic() - started) * 1000, 1),
                "reason": reason,
                "ceiling": task_ceiling(_sampler.snapshot()),
                "queue_position": max(0, len(self._waiting) - 1),
            }

        deadline = started + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _reject("wait_timeout")
                if abort_check is not None and abort_check():
                    return _reject("client_gone")

                ev = self._event()
                # 顺序很关键：**先 clear 再判据**。若反过来（判据→clear→wait），
                # 在"判据失败"与"clear"之间到达的 release 会被清掉 → 丢失唤醒。
                ev.clear()
                if self._try_take(task_id):
                    waited = round((time.monotonic() - started) * 1000, 1)
                    self._last_wait_ms = waited
                    self._max_wait_ms = max(self._max_wait_ms, waited)
                    return {
                        "admitted": True, "waited_ms": waited, "reason": "",
                        "ceiling": task_ceiling(_sampler.snapshot()), "queue_position": 0,
                    }
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(remaining, self.POLL_S))
                except (asyncio.TimeoutError, TimeoutError):
                    pass  # 超时回到循环顶部重判（限额可能已变）
        finally:
            # 同步清理，取消作用域里也能执行完
            with self._lock:
                if task_id in self._waiting:
                    self._waiting.remove(task_id)

    def release(self, task_id: str) -> None:
        """释放槽位并唤醒等待者。

        **同步方法**：这是有意的——调用点在 SSE 生成器的 `finally` 中，
        客户端断连时该作用域已被取消，async release 会因 await 立即再抛
        CancelledError 而永不执行，导致槽位永久泄漏。
        """
        with self._lock:
            if task_id in self._active:
                self._active.remove(task_id)
        self._wake()

    def try_acquire(self, task_id: str) -> bool:
        """同步快路径：有空位立即占位返回 True；否则 False（**不进等待队列**）。

        供 SSE 入口在排队前先用一下——单用户/资源充裕时零开销（不建 asyncio
        任务、不发排队事件），与今天的行为完全一致。
        """
        return self._try_take(task_id)

    # ---------------------------------------------------------------- 预算

    def clamp_fp_concurrency(self, configured: int) -> int:
        """把用户配置的 FP 并发度收敛到当前资源预算内。

        `slots // 活跃任务数` = 每个任务分到的推理份额。
        → **单任务时等于不收敛**（与 v3.5.5 以来行为一致，无回归）；
        → 多任务时自动平分：3 份额 2 任务 → 各 1。
        """
        active = max(1, len(self._active))
        per_task = max(1, inference_slots() // active)
        return max(1, min(int(configured), per_task))


# ============================================================================
# 单例
# ============================================================================

_sampler = _ResourceSampler()
_gate = GenerationGate()


class _ResourceGuard:
    """进程级单例门面：把探测、预算、准入门收在一个入口。"""

    @property
    def gate(self) -> GenerationGate:
        return _gate

    def start(self) -> None:
        """启动资源采样线程（幂等）。"""
        if _sampler.is_alive():
            return
        _sampler.start()
        logger.info(
            "[ResourceGuard] 资源监控已启动: CPU 逻辑核={}, 物理核={}, 推理份额={}, 采样={}s",
            int(cpu_limit()), physical_cores(), inference_slots(), _SAMPLE_INTERVAL,
        )

    def stop(self) -> None:
        _sampler.stop()

    # -- 预算 --

    def task_ceiling(self) -> int:
        return task_ceiling(_sampler.snapshot())

    def clamp_fp_concurrency(self, configured: int) -> int:
        return _gate.clamp_fp_concurrency(configured)

    def active_tasks(self) -> int:
        return _gate.active_tasks

    def waiting_tasks(self) -> int:
        return _gate.waiting_count

    # -- 快照 --

    def cpu_snapshot(self) -> dict:
        return _sampler.snapshot()

    def stats(self) -> dict:
        """全量指标（供仪表盘详情端点与 /api/health 复用）。"""
        snap = _sampler.snapshot()
        return {
            "cpu": {
                "logical_cores": int(cpu_limit()),
                "physical_cores": physical_cores(),
                "cpu_limit": cpu_limit(),
                "usage_percent": snap.get("cpu_percent"),
                "usage_percent_avg": snap.get("cpu_percent_avg"),
                "load_1m": snap.get("load_1m"),
                "load_5m": snap.get("load_5m"),
                "load_15m": snap.get("load_15m"),
            },
            "memory": snap.get("memory") or {},
            "inference_slots": inference_slots(),
            "sampled_at": snap.get("sampled_at", ""),
            "sampler_error": snap.get("error", ""),
            "generation": _gate.snapshot(),
        }

    def health_brief(self) -> dict:
        """极轻量字段（纯内存计数，不读 /proc），可随 /api/health 每次探活返回。"""
        return {
            "cpu_logical_cores": int(cpu_limit()),
            "cpu_physical_cores": physical_cores(),
            "inference_slots": inference_slots(),
            "task_ceiling": self.task_ceiling(),
            "active_tasks": _gate.active_tasks,
            "waiting_tasks": _gate.waiting_count,
        }


resource_guard = _ResourceGuard()
