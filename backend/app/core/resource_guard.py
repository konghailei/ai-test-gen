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
   - CPU 逻辑核：cgroup v2 `cpu.max` → cgroup v1 quota/period →
     `sched_getaffinity` → cgroup cpuset 兜底 → `cpu_count`。
     **容器配额优先**——`nproc` 在容器里看到的是宿主逻辑核，不一定是自己能用的。
   - CPU 物理核：读 `/proc/cpuinfo` 的 `siblings / cpu cores` 得**每核线程数**，
     再 `ceil(可用逻辑核 / 每核线程数)`（多 socket 求和口径一致）。
     读不到拓扑才退回「逻辑核 ÷ 2」保守估计（见 `physical_cores`）。
     与 `db/milvus_store._physical_cores` **同口径**。
   - 内存：cgroup `memory.max/current` → `/proc/meminfo`。容器有上限时
     **以 cgroup 为准**——那才是真正的 OOM 线（宿主 MemTotal 会把"容器快饿死"
     误判成"内存充足"）。
   - CPU 使用率：`/proc/stat` 差值 + 10s 窗口平均（瞬时值对排程毫无意义）。

   ⚠️ 为什么必须放后台线程：读 /proc 是**同步 IO**，S1 立下的纪律就是"同步 IO
   不进事件循环"。而且并发预算要在等待循环里**每秒重判**，若每次都现读会把
   探测本身变成阻塞源。故采样线程 2s 刷新一次缓存，预算判定只读内存。

2. **并发预算**（`inference_slots`）：本地推理能占的"物理核份额"。

   唯一口径（v4.1.7 定稿，**顺序不可换**）：
      可用逻辑核 = `effective_cpu_limit()`（自身额度 − 兄弟容器投影）
      可用物理核 = `physical_cores_from_logical(可用逻辑核)`
      slots      = `_reserve_for_main_process(可用物理核)`

   **"留 1 核给主进程"只在这一处扣，且小机器（1 物理核）不扣。**
   v4.1.6 的写法是 `physical_cores() - 1` 得基础值、投影在 `task_ceiling()`
   里再压一次，等于同一个"留 1 核"扣两遍——6 逻辑核的内网机器上
   `2 − 1 = 1`，明明能吃 2 个生成任务却只放 1 个。

   **口径分界（v4.1.5 明确）**：
   - **并发任务数**按**物理核**算——超线程对 matmul 是纯竞争（8 线程比 4 线程
     慢 22%，docs/27），并发粒度落在物理核上才对。
   - **单次推理的线程数**（`encode_threads`）按**逻辑核**封顶、实测最优 4——
     torch 默认是"物理核数"，与容器配额无关，必须显式设（`apply_torch_threads`）。
   - **负载判据**（`task_ceiling`）比较基准是**逻辑核**，因为 `load_1m` 的可运行
     队列按逻辑 CPU 统计，拿物理核去比会让降级门过早触发。

   - 全局生成任务上限 `task_ceiling()` = min(inference_slots, 配置硬上限)，
     再按**运行时负载降级**（CPU 打满或内存紧张时收紧）。
   - 每任务 FP 并发 `clamp_fp_concurrency(cfg)` = min(cfg, slots // 活跃任务数)。
     → **单任务时与今天完全一致**（无回归），多任务时自动平分预算。

   ⚠️ **`inference_slots()` 依赖 `effective_cpu_limit()` 读投影**，而投影值
   在部署后可能变化（改环境变量/落盘文件需重启容器）。因此它是一个
   "配置态"的量，不是"实时态"；实时态只有 `task_ceiling()` 的负载降级。

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
import math
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
#: 单次 encode 实测最优 intra-op 线程粒度（docs/27 §2.6：4 线程 35.2s vs 8 线程 42.9s）
_BEST_ENCODE_THREADS = 4

#: **[部署侧投影]** 同机兄弟容器（celery）占用的逻辑核数。
#: 为什么用**环境变量 + 落盘文件**而不是进程内 dict：
#:   `docker exec` 起的是**另一个进程**，进程内状态写进去 uvicorn 主进程看不到
#:   （实测踩到：脚本打印"投影已生效"，但主进程 ceiling 根本没变）。
#: 环境变量在容器重建时由 compose 注入 → 主进程一启动就能读到。
#: 落盘文件是给"容器已建好、只想改投影"的场景用的（改完重启容器生效）。
_PEER_ENV_KEYS = ("PEER_CELERY_CPU", "PEER_CELERY_CPU_CORES")
#: 落盘路径（容器内）；宿主机 ./data 已挂到 /app/data
_PEER_FILE_CANDIDATES = ("/app/data/.peer_cpu_budget", "data/.peer_cpu_budget")
#: 落盘结果的**一次性缓存**：`(核数, 来源)`。
#: 为什么必须缓存：`health_brief()` 被 `/api/health` 每次探活调用，若每次都 `open()`
#: 就是**同步 IO 进事件循环**（本项目已因同类问题吃过亏）。而投影是部署期确定的静态事实
#: （改了要重启容器），启动后第一次读到就可以定下来。环境变量分支不缓存（零成本，且便于
#: 现场 `docker run -e` 试验）。
_PEER_FILE_CACHE: dict[str, Any] = {}

#: CPU 拓扑（逻辑核 / 每核线程数），进程内只探测一次——容器配额启动后不会变，
#: 而 `physical_cores()` 会被 admin 重复调用，不该反复读 /proc 与 /sys。
#: `threads_per_core` 的值可能是 None（拓扑不可读/不可信），**None 也要缓存**，
#: 否则每次调用都会重读 /proc/cpuinfo。
_TOPOLOGY: dict[str, Any] = {}
_TOPOLOGY_LOCK = threading.Lock()


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


def _cgroup_v1_cpu_limit(base: str = "/sys/fs/cgroup/cpu") -> float | None:
    """cgroup v1：quota/period 两个文件，quota 为 -1 表示无限制。

    Args:
        base: v1 cpu 控制器目录。参数化只为可测（tmp 目录伪造 v1 布局）。
    """
    q_raw = _read_text(f"{base}/cpu.cfs_quota_us")
    p_raw = _read_text(f"{base}/cpu.cfs_period_us")
    if not q_raw or not p_raw:
        return None
    try:
        q, p = float(q_raw), float(p_raw)
        if q <= 0 or p <= 0:
            return None
        return q / p
    except ValueError:
        return None


def _cpuset_count(base: str = "/sys/fs/cgroup") -> int | None:
    """cgroup cpuset 约束的核数；无 cpuset 文件/内容为空/覆盖全部核时返回 None。

    为什么需要（3.10 与 4.x 都要）：
        `cpu.max` / `cpu.cfs_quota_us` 只表达"时间份额"，不表达"能用哪几个核"。
        开 `--cpuset-cpus=0-3` 或 systemd `AllowedCPUs=` 时二者都可能是 unlimited，
        此时 `sched_getaffinity` 才是唯一正确来源。顶层先试 `os.sched_getaffinity`
        （内核已按 cpuset 收敛过，一行搞定），这里只是**兜底**：极老的 3.10 分支上
        Python 层拿到的 affinity 可能未收敛到 cgroup 范围。

    v1/v2 文件位置不同，逐个探测（谁先读到用谁）：
        v2: /sys/fs/cgroup/cpuset.cpus.effective（内核算好，最准）
        v1: /sys/fs/cgroup/cpuset/cpuset.effective_cpus（部分内核有）
            /sys/fs/cgroup/cpuset/cpuset.cpus（原始配置，可能含换行续写）
    """
    for rel in ("cpuset.cpus.effective", "cpuset/cpuset.effective_cpus", "cpuset/cpuset.cpus"):
        raw = _read_text(f"{base}/{rel}")
        if not raw:
            continue
        n = _count_cpu_list(raw)
        if n:
            return n
    return None


def _count_cpu_list(raw: str) -> int | None:
    """解析 `0-3,8,10-11` 形式的 CPU 列表为核数；解析失败返回 None。"""
    total = 0
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if "-" in chunk:
                lo, hi = chunk.split("-", 1)
                lo_i, hi_i = int(lo), int(hi)
                if hi_i < lo_i:
                    return None
                total += hi_i - lo_i + 1
            else:
                int(chunk)
                total += 1
        except ValueError:
            return None
    return total or None


def _affinity_count() -> int | None:
    """Linux 可用核数（考虑 cpuset/taskset 收缩）；非 Linux 平台无此 API。"""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None


def cpu_limit() -> float:
    """本进程真正能用的 CPU 核数（浮点，容器配额可能是 2.5 这种）。

    优先级：cgroup quota → affinity → cpuset 兜底 → cpu_count。
    ⚠️ 不能只用 `os.cpu_count()`：容器里它返回**宿主**逻辑核，
       而 cgroup 配额可能只给了 2 核——按 8 核定并发会把机器打穿。
    affinity 放在 cpuset 兜底之前：`sched_getaffinity` 已包含 cpuset 收敛结果，
       正常情况下前面就返回了；cpuset 兜底只为"affinity 不可用/未收敛"的边角。
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
    cs = _cpuset_count()
    if cs and cs > 0:
        return float(cs)
    return float(os.cpu_count() or 1)


def _cpu_snapshot_source() -> str:
    """标记 `cpu_limit()` 最终命中哪条探测链（观测用，只有几个小文件读取）。"""
    try:
        for name, probe in (("cgroup_v2", _cgroup_v2_cpu_limit), ("cgroup_v1", _cgroup_v1_cpu_limit)):
            try:
                if (probe() or 0) > 0:
                    return name
            except Exception:  # noqa: BLE001
                continue
        if (_affinity_count() or 0) > 0:
            return "affinity"
        if (_cpuset_count() or 0) > 0:
            return "cpuset"
    except Exception:  # noqa: BLE001
        return "unknown"
    return "cpu_count"


def physical_cores() -> int:
    """本进程所在容器**真正能用的物理核数**（多路 socket 求和）。

    为什么不能再用「逻辑核 ÷ 2」：
        ① 那是硬编码假设"开了超线程"。若宿主关超线程 / ARM 大核簇 /
           `--cpus` 给出非偶数份额，÷2 直接算错——本机 6 核配额下会报 3，
           实际拿到的是 6 个逻辑核背后 4 个物理核里的 6 个硬件线程。
        ② 它从不读 `siblings` / `cpu cores`，等于"猜"而不是"测"。

    现在的口径（逐级降级，任一级成功即返回）：
        1. 读 `/proc/cpuinfo` 的 `siblings` 与 `cpu cores` → 每核线程数 →
           `ceil(可用逻辑核 / 每核线程数)`。**这是唯一一次读真实拓扑**，
           结果缓存在 `_TOPOLOGY`。
        2. 拓扑读不到（非 x86 / 容器屏蔽 /proc/cpuinfo）→ 退回「逻辑核 ÷ 2」
           保守估计（宁少不多，超卖比欠卖危险）。
        3. 仍然至少 1——绝不返回 0（否则并发预算算出 0，生成永远进不来）。

    ⚠️ 与 `db/milvus_store._physical_cores` **同口径**：那边靠它决定去重进程池
    worker 数，这边靠它决定生成并发。两处必须一致，否则"去重开 3 进程 × 4 线程"
    与"同时跑 N 个生成任务"会互相不知道对方在抢核。
    """
    lc = int(cpu_limit())
    # 1) 真实拓扑
    try:
        per_core = _threads_per_core()
        if per_core and per_core > 0:
            return max(1, -(-lc // int(per_core)))  # ceil 除法
    except Exception:  # noqa: BLE001 - 拓扑探测失败不阻断预算
        pass
    # 2) 保守降级：假设开了超线程
    return max(1, (lc or 2) // 2)


def _threads_per_core() -> int | None:
    """每物理核的硬件线程数；读不到/不可信时返回 None。

    一次解析 `/proc/cpuinfo` 同时取两个字段（都是"每个 processor 一行"：
        8 cpu cores   : 4      ← 每 socket 的物理核数
        8 siblings    : 8      ← 每 socket 的硬件线程数
    8 行 = 8 个逻辑处理器，说明是 1 socket × 4 核 × 2 线程。
    `siblings / cpu cores` 即每核线程数，**跨 socket 求和也一样**
    （2 socket × 8 核 × 2 线程：siblings=16/cores=8 → 仍是 2）。

    ⚠️ 必须做**交叉校验**（v4.1.5 补充，由反向审查发现）：
        有些环境把 `cpu cores` 填成**整机总核数**而非每 socket 核数，
        于是 `siblings == cores` → tpc=1 → `physical_cores(8) = 8`，
        **并发预算翻倍**，比原来的 ÷2 更危险。但"tpc=1"本身也可能是真的
        （关超线程的机器就是 1），**不能一概拒绝**。

        真正的判据是**自相矛盾**：
          ① `tpc ∈ {1,2,4,8}`——SMT 的现实取值，3/5/6 之类不可能
          ② `n_proc % siblings == 0`——整机逻辑处理器数必须是每 socket 线程数的整数倍
             （1 socket: n=8/sib=8 ✔；2 socket: n=16/sib=8 ✔；矛盾: n=2/sib=8 ✘）
          ③ 若 cpuinfo 给出 physical id，则 `n_proc / siblings == len(physical ids)`
        三条全过才采信；任一不过 → None → 退回 ÷2（宁少不多，超卖比欠卖危险）。

        反例说明：`n_proc=8, siblings=8, cores=8` 三条全过 → 视为真实
        "1 socket × 8 核 × 无超线程"，`physical_cores=8` 是**正确**的。
        这类机器本来就能开 8 个并发任务，不是 bug。

    结果缓存：容器配额与宿主拓扑在进程生命周期内不变。
    """
    with _TOPOLOGY_LOCK:
        if "threads_per_core" in _TOPOLOGY:
            return _TOPOLOGY["threads_per_core"]
    tpc: int | None = None
    raw = _read_text("/proc/cpuinfo")
    if raw:
        siblings: int | None = None
        cores: int | None = None
        n_proc = 0
        physical_ids: set[str] = set()
        for line in raw.splitlines():
            if line.startswith("processor"):
                n_proc += 1
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "siblings":
                try:
                    siblings = int(v)
                except ValueError:
                    siblings = None
            elif k == "cpu cores":
                try:
                    cores = int(v)
                except ValueError:
                    cores = None
            elif k == "physical id":
                physical_ids.add(v)
        if siblings and cores and siblings >= cores > 0:
            cand = max(1, siblings // cores)
            ratio_ok = cand in (1, 2, 4, 8)
            div_ok = n_proc <= 0 or (n_proc % siblings == 0)
            sockets = (n_proc // siblings) if (n_proc and siblings and div_ok) else None
            id_ok = (not physical_ids) or sockets is None or sockets == len(physical_ids)
            if ratio_ok and div_ok and id_ok:
                tpc = cand
            else:
                logger.debug(
                    "[ResourceGuard] cpuinfo 拓扑自相矛盾（siblings={}, cores={}, 处理器数={}, "
                    "socket标识={}）→ 退回逻辑核÷2 保守估计",
                    siblings, cores, n_proc, sorted(physical_ids)[:3],
                )
    with _TOPOLOGY_LOCK:
        _TOPOLOGY["threads_per_core"] = tpc
    return tpc


def _topology_source() -> str:
    """物理核数是怎么来的：`cpuinfo`（真拓扑）/ `heuristic`（÷2 保守估）。"""
    try:
        return "cpuinfo" if _threads_per_core() else "heuristic"
    except Exception:  # noqa: BLE001
        return "heuristic"


def _read_peer_cpu_budget() -> tuple[float, str]:
    """读出兄弟容器占用（逻辑核数, 来源）；无投影返回 (0.0, "")。

    读取顺序（后者覆盖前者，便于现场临时改）：
      ① 环境变量 `PEER_CELERY_CPU`（compose 注入，容器重建即生效）
      ② 落盘文件 `/app/data/.peer_cpu_budget`（首行一个数字，改完重启容器生效）

    ⚠️ 为什么必须走这两条而不是进程内 dict：`docker exec` 是**另一个进程**，
    往里写 dict 主进程永远看不到（实测踩到：脚本打印"投影已生效"，ceiling 没变）。
    """
    raw, src = "", ""
    for key in _PEER_ENV_KEYS:
        v = (os.environ.get(key) or "").strip()
        if v:
            raw, src = v, f"env:{key}"
            break

    # env 通道命中：直接解析返回（**不缓存**，零成本，便于现场 `docker run -e` 试验）。
    # ⚠️ v4.1.6 修复：这里原先漏了 return —— env 命中后函数一路走到末尾隐式返回 None，
    #    调用方 `[0]` 就炸 `TypeError: 'NoneType' object is not subscriptable`，
    #    表现成 `/api/health` 500（比"投影不生效"严重得多）。
    #    教训：给函数加分叉时，**每条分叉都必须显式 return**；类型标注是 tuple
    #    不代表运行时会替你兜底。
    if raw:
        try:
            val = float(raw)
        except ValueError:
            logger.debug("[ResourceGuard] 兄弟容器 CPU 份额无法解析（{}={}），忽略", src, raw[:20])
            return 0.0, ""
        if val <= 0:
            return 0.0, ""
        return val, src

    # 落盘通道只读一次（见 `_PEER_FILE_CACHE` 的注释：health 探活不能做同步 IO）
    if _PEER_FILE_CACHE:
        return float(_PEER_FILE_CACHE.get("cores") or 0.0), str(_PEER_FILE_CACHE.get("source") or "")
    for path in _PEER_FILE_CANDIDATES:
        txt = _read_text(path)
        if txt:
            raw, src = txt.splitlines()[0].strip(), f"file:{path}"
            break
    # 文件不存在也缓存（否则每次探活都去 stat 两个路径）；投影要变更必须重启容器，
    # 与"容器创建时注入环境变量"的语义一致。
    if not raw:
        _PEER_FILE_CACHE.update({"cores": 0.0, "source": ""})
        return 0.0, ""
    try:
        val = float(raw)
    except ValueError:
        logger.debug("[ResourceGuard] 兄弟容器 CPU 份额无法解析（{}={}），忽略", src, raw[:20])
        _PEER_FILE_CACHE.update({"cores": 0.0, "source": ""})
        return 0.0, ""
    if val <= 0:
        _PEER_FILE_CACHE.update({"cores": 0.0, "source": ""})
        return 0.0, ""
    _PEER_FILE_CACHE.update({"cores": val, "source": src})
    return val, src


def set_peer_cpu_budget(logical_cores: float | None, source: str = "") -> bool:
    """把「同机兄弟容器的 CPU 占用」持久化，供**主进程**下次启动时读取。

    背景（内网小机器实证的风险）：
        `ai-test-gen-api` 与 `ai-test-gen-celery` 是**两个容器**，各自读自己的
        cgroup。谁都只看得到自己那份额度——api 算出 `逻辑核=6 → 物理核=3 → slots=2`，
        celery 也算一份，**合计超出整机容量**。celery 跑 embedding + Milvus 双写，
        它那份预算 api 完全看不见。

    做法：写落盘文件（`/app/data/.peer_cpu_budget`，落在已挂载的 ./data 上），
    主进程启动时由 `_read_peer_cpu_budget()` 读入。**不做 cgroup 改动**，可审计、可删。

    ⚠️ 本函数**只对"即将启动的新进程"生效**，不会改变当前正在跑的进程
    （原因见 `_read_peer_cpu_budget` 的进程隔离说明）。部署脚本的执行顺序因此是
    「先写文件 → 再 up -d 重建容器」。

    Args:
        logical_cores: 兄弟容器**占用的**逻辑核数；`None`/`<=0` = 撤销投影。
        source: 记录来源（写进 stats，便于现场核对是谁设的）。

    Returns:
        是否成功写入/删除（失败返回 False，调用方应告警但不阻断）。
    """
    path = None
    for cand in _PEER_FILE_CANDIDATES:
        d = os.path.dirname(cand) or "."
        if os.path.isdir(d):
            path = cand
            break
    try:
        if logical_cores is None or float(logical_cores) <= 0:
            if path and os.path.exists(path):
                os.remove(path)
            _PEER_FILE_CACHE.update({"cores": 0.0, "source": ""})
            return True
        if not path:
            logger.warning("[ResourceGuard] 找不到可写的投影目录（data/ 未挂载？），投影未落盘")
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{float(logical_cores)}\n# source={source or 'deploy.sh'}\n")
        # 同进程写入后立即刷新缓存，避免本进程后续读到旧值（跨进程仍需重启）
        _PEER_FILE_CACHE.update({"cores": float(logical_cores), "source": f"file:{path}"})
        logger.info(
            "[ResourceGuard] 兄弟容器 CPU 占用已落盘: {} 逻辑核 → {}（来源: {}），"
            "主进程重启后按「自身 − 该值」计预算",
            float(logical_cores), path, source or "deploy.sh",
        )
        return True
    except Exception as e:  # noqa: BLE001 - 投影失败不影响主流程
        logger.warning("[ResourceGuard] 写投影失败（忽略）: {}", str(e)[:120])
        return False


def _own_logical_cores() -> float:
    """本容器自己的逻辑核额度（**不含**兄弟容器扣减）。

    单独抽出来是因为 `inference_slots()` 需要同时知道"我自己的额度"和
    "兄弟占了多少"，才能分别用不同的取整方向折算（见该函数）。若直接用
    `effective_cpu_limit()`（已经减过了），就只能对"净额"做一次二选一的取整，
    无论向上还是向下都会在 4~6 核的小机器上算错半个核。
    """
    try:
        return max(1.0, float(cpu_limit()))
    except Exception:  # noqa: BLE001
        return 1.0


def effective_cpu_limit() -> float:
    """本进程**实际可用于本地推理**的逻辑核数（自身额度 − 兄弟容器占用）。

    为什么是**减法**而不是取 min：
        `min(自身, 兄弟份额)` 的语义是"兄弟份额是上限"，但兄弟容器是**在抢核**，
        不是"给你留了这么多"。举例：api 被限 8、celery 被限 2，
        取 min 得 2（错，api 明明还有 6 核可跑），减法得 6（对）。

    为什么不直接改 `cpu_limit()`：那是"容器能看到的额度"，是**环境事实**；
    这里算的是"同机竞争后还能用多少"，是**预算**。两者不能混，否则
    自检日志（mem_source/cpu_source 那套证据）会跟着失真。

    ⚠️ 这是**逻辑核**口径，只用于展示与负载判据。算并发任务数请走
    `inference_slots()`——它还要按物理核折算、并让兄弟那份向下取整。
    """
    own = _own_logical_cores()
    peer, _src = _read_peer_cpu_budget()
    # 下限 1：绝不返回 0 或负数（否则并发预算算出 0，生成永远进不来）
    return max(1.0, own - float(peer or 0))


def physical_cores_from_logical(logical: float) -> int:
    """把**可用逻辑核数**折算成物理核数（向上取整，至少 1）。

    唯一的折算入口：`inference_slots()` 与 `task_ceiling()` 的投影分支都必须
    走这里。此前两处各写一份 `ceil(x / tpc)`，一旦其中一处改了取整方向，
    "基础值与投影值不可比"的问题会以"偶尔多放一个任务"的形式静默出现。

    向上取整的理由：算的是"**自己**能承载几个并发任务"。宁可多算半核也不能
    因为 3 逻辑核 ÷ 2 = 1.5 白丢一个任务——那半个核实际就在自己手里。
    """
    tpc = _threads_per_core() or 2
    try:
        n = int(-(-float(logical) // float(tpc)))
    except Exception:  # noqa: BLE001
        n = int(logical or 1)
    return max(1, n)


def physical_cores_claimed(logical: float) -> int:
    """把**兄弟容器占用的逻辑核数**折算成物理核数（**向下取整**）。

    取整方向必须与 `physical_cores_from_logical` **相反**，否则两边都 +0.5
    会把整机核数卖超。v4.1.7 实测内网机器（6 逻辑核 / 3 物理核，超线程 2）：
        celery 占 3 逻辑核 → ceil 得 2 个物理核（实际只用 1.5 个）
        api 可用 3 逻辑核 → ceil 也得 2 个物理核
        2 + 2 = 4 > 整机 3 个物理核 → **超卖**，于是留 1 核后 slots 只剩 1
    改为：兄弟向下取整（3→1 个物理核），api 向上取整（3→2），
    合计 3 = 整机核数，api 拿到 2 个物理核 → 留 1 核 → **slots=2**。

    语义上更准确：兄弟"至少占掉"的整物理核才算被拿走，剩下的碎片归 api。
    宁可用满碎片，也不要因为"半个核不知道算谁的"而白空着。
    """
    tpc = _threads_per_core() or 2
    try:
        n = int(math.floor(float(logical) / float(tpc)))
    except Exception:  # noqa: BLE001
        n = int(logical or 1)
    return max(0, n)


def inference_slots() -> int:
    """本地模型推理可用的"物理核份额"（留 1 核给主进程/事件循环）。

    为什么按**物理核**而不是逻辑核分配并发：超线程对 transformer matmul 是纯
    资源竞争，8 线程实测比 4 线程**慢 22%**（docs/27 §2.6）。并发任务的粒度必须
    落在物理核上，逻辑核只用来设**单次 encode 的线程数**（见 encode_threads）。

    ## ⚠️ 这里**不扣**兄弟容器投影（v4.1.7 返工结论）

    本函数有两个消费者，对"兄弟占用"的容忍度完全不同：

      ① `task_ceiling()` —— 全局**并发生成任务数**。多个任务同时跑起来会一起
         抢核，超卖代价大 → **必须扣**兄弟份额。
      ② `clamp_fp_concurrency()` —— **单个任务内**的 FP 并行度。celery 是异步
         旁路（只在知识库入库时跑 embedding），**大部分时间空闲**。因为它"声明
         了 3 核配额"就永久砍掉 api 一路并行，是常态性浪费。

    实测代价（本机 8 逻辑核 / api cpus=6 / celery cpus=3）：
        扣 → slots=1 → 单任务 FP 并行度被压到 1（生成变慢一倍）
        不扣 → slots=2 → FP 并行度 2（v4.1.6 行为，无回归）
    `test_fp_concurrency.py::test_concurrency_semaphore_respected` 正是因此变红，
    它测的是"限流逻辑本身"，不该被核数预算间接影响。

    投影扣减统一放在 `task_ceiling()` 里，见那里的单位说明。
    """
    return _reserve_for_main_process(physical_cores())


def _reserve_for_main_process(phys: int) -> int:
    """从物理核份额里给主进程/事件循环留 1 核，但小机器上不扣。

    小机器（折算后物理核 = 1，即 2~4 逻辑核）的取舍完全不同：
        扣掉一核 = 并发 0 → 被 `max(1, ...)` 兜成 1，等于"扣了但没生效"；
        而那一核留给谁？主进程在**等 LLM 返回**时几乎不吃 CPU（实测 api 容器
        1~1.6%），本地推理的 3~4 线程也塞得进 1 个物理核。
        → 小机器上"1 个能跑的并发"远比"1 个空闲核"有价值，故不扣。

    大机器（≥2 物理核）照旧扣 1：那里并发本来就不止 1，留 1 核能真正
    让事件循环不被 matmul 挤住，代价只是少一个并发。

    这也是"6 核机器只允许 1 个生成任务"的直接修复——原来扣完之后
    `3-1=2`，再被投影压一次成 1；现在 `物理 2 核算出的 slots=2`。
    """
    phys = max(1, int(phys))
    if phys <= 1:
        return 1
    return phys - 1


def encode_threads() -> int:
    """单次 encode / 本地模型推理的 **intra-op 线程数**（torch.set_num_threads）。

    为什么必须显式设置：torch 默认是 `物理核数`（不是容器配额），容器被限制到
    6 逻辑核时默认仍可能开满宿主硬件线程 → 线程池持有物理核却只分到时间片，
    上下文切换吃掉全部收益。实测最优点固定在 4 线程（docs/27）。

    取值 = `max(1, min(4, 物理核, 逻辑核))`，三个上限各自的理由：
        - `4`：实测最优粒度，超过无收益（8 线程反而慢 22%）
        - **物理核**：超线程的两条硬件线程共享执行单元，按物理核开才不互相争抢
        - **逻辑核**：容器只给 2 核时开 4 线程纯属自伤（超配只会增加切换开销）
    """
    try:
        phys = physical_cores()
        lc = int(cpu_limit())
    except Exception:  # noqa: BLE001
        return _BEST_ENCODE_THREADS
    return max(1, min(_BEST_ENCODE_THREADS, phys, lc))


def apply_torch_threads() -> int:
    """把 `encode_threads()` 落到 torch 线程池（进程级，幂等）。

    **必须在 API 主进程启动时调用**：此前只有去重进程池的 worker init 里设了
    `set_num_threads(4)`，主进程完全没设 → 主进程 encode 用的是 torch 默认值
    （= 物理核数，与容器配额无关），这是"并发预算算得再准也白算"的隐藏漏口。
    torch 未安装（纯 API 形态）时静默跳过——不引入 torch 依赖。

    Returns:
        最终生效的 intra-op 线程数（无 torch 时返回"本应设置的值"，
        供调用方/单测断言——函数本身失败也不抛异常）。
    """
    n = encode_threads()
    try:
        import torch as _torch  # noqa: PLC0415 - 可选依赖，延迟导入

        _torch.set_num_threads(n)
        interop_ok = True
        try:
            # inter-op 线程池按"算子并行度"开线程，本场景是单流推理，
            # 多开会与 intra-op 抢核（torch 默认值随核数增长）。
            _torch.set_num_interop_threads(1)
        except Exception:  # noqa: BLE001 - 已启动/不支持时不可改，忽略
            interop_ok = False
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        # 用 info 级：排查"线程数为什么是 N"必须能直接看到，
        # 而不是猜（debug 级在生产默认不输出 = 又一处静默）。
        logger.info(
            "[ResourceGuard] torch 线程已设定: intra-op={} interop={} (物理核={}, 逻辑核={}, 拓扑={})",
            n, 1 if interop_ok else "默认", physical_cores(), int(cpu_limit()), _topology_source(),
        )
    except Exception as e:  # noqa: BLE001 - 无 torch 时保持默认，不影响启动
        logger.info(
            "[ResourceGuard] 未设置 torch 线程（{}）→ 本应设为 {}（物理核={}, 逻辑核={}）",
            str(e)[:60], n, physical_cores(), int(cpu_limit()),
        )
    return n


def _mem_from_cgroup_v2() -> tuple[int, int] | None:
    """cgroup v2 内存（total, available）字节数；无上限时返回 None。

    v2 只有 unified 层级：`memory.max` / `memory.current` 就在根下。
    """
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


#: cgroup v1 的「无限制」哨兵：limit_in_bytes 为极大值（PAGE_COUNTER_MAX 量级）。
#: 内核用 `LONG_MAX & PAGE_MASK` 表示 unlimited，**位宽决定具体数值**：
#:   - 64 位: ≈ 2^63 ≈ 9.2EB
#:   - 32 位（i686/armhf 的 CentOS 7）: ≈ 2^31 ≈ 2.1GB
#: 32 位的值**和"真的给了 2GB 上限"靠量级区分不了**，所以下面用精确等式判定。
_CGROUP_V1_UNLIMITED_FLOOR = 1 << 60

#: 32 位内核的 unlimited 精确值：`(LONG_MAX & PAGE_MASK)`，page 4K。
#:   0x7FFFFFFF & ~0xFFF = 0x7FFFF000 = 2147479552
#: 实测（2026-10-27）：若只按 1<<60 判，2.1GB 会被当成真实上限 → 占用 300MB 时
#: ratio 已是 0.86，稍涨即跌破 MEM_LOW_RATIO=0.15 → 并发被压到 1、排队超时，
#: 而 mem_source 仍显示 cgroup_v1（"看起来正常"），是最难归因的一类降级。
_CGROUP_V1_UNLIMITED_32BIT = 2147479552
#: 判定容差只给一页：该值本质是 `2^31 - 一页`，不同 page size(4K/16K/64K) 会差几页。
#: 若放大到 MB 级，会把真实配额 2GB(=2^31) 一起误判 —— 实测已踩：
#: 2GB 与哨兵只差 4096B，容差 1MB 时真实 2G 容器被当成"无限制"→ OOM 保护反而失效。
_CGROUP_V1_UNLIMITED_TOLERANCE = 65536  # 64KB = 最大 page size


def _mem_from_cgroup_v1(base: str = "/sys/fs/cgroup/memory") -> tuple[int, int] | None:
    """cgroup v1 内存（total, available）字节数；无上限时返回 None。

    为什么必须有这条分支（2026-10-27 实证）：
        Kernel 3.10（RHEL/CentOS 7 系列）**只有 cgroup v1**，没有 `memory.max`。
        此前 `_mem_from_cgroup` 只读 v2 → 在 3.10 上恒返回 None → 静默退化为
        `/proc/meminfo`（宿主总内存），导致 `MEM_LOW_RATIO` 的 OOM 保护完全失效：
        容器内存快耗尽时守卫仍认为"内存充足"，最终被内核 OOM Killer 直接杀掉
        （比"慢"致命得多）。CPU 侧早已有 v1 兜底（`_cgroup_v1_cpu_limit`），
        内存侧此前是漏的 —— 同一条链上不能只兜一半。

    可用内存口径：v1 没有 v2 的 `max-current` 直接可得，也没有 `meminfo`。
    这里用 PG 同款思路的保守估算：`limit - usage`，再与 `total - working_set`
    取小（working_set = usage - inactive_file，是内核认为"不可回收"的部分）。
    取小是因为 available 被低估只会让守卫更早降并发（安全侧），
    高估才会漏掉 OOM 保护（危险侧）——fail-safe 优先。

    Args:
        base: cgroup v1 memory 控制器目录。参数化只为可测：单测用 tmp 目录
              伪造 v1 文件布局，不必真的跑在 3.10 内核上。
    """
    lim_raw = _read_text(f"{base}/memory.limit_in_bytes")
    usage_raw = _read_text(f"{base}/memory.usage_in_bytes")
    if not lim_raw or not usage_raw:
        # 非 v1 环境（或未开 memory controller）→ 交给上层走下一个探测源
        return None
    try:
        total_b = int(lim_raw)
        used_b = int(usage_raw)
    except ValueError:
        return None
    if total_b <= 0 or total_b >= _CGROUP_V1_UNLIMITED_FLOOR:
        # 关键：v1 的"无限制"不是字符串 "max"，而是个巨大数值。
        # 不判这个哨兵会把 9.2EB 当成真实上限，得出 ratio≈1.0 的"内存极度充足"。
        return None

    # 32 位哨兵：unlimited 恰好是 LONG_MAX&PAGE_MASK ≈ 2GB，量级上看不出异常。
    # 判据必须同时满足两条，否则会误伤真实配额：
    #   ① 落在哨兵的极窄窗口内（±64KB，覆盖不同 page size）
    #   ② **不是** 2 的整数幂（真实配额都是 1G/2G/4G 这类规格值，哨兵是 2^31-一页）
    # 实测踩坑：只放宽容差会把真实 2GB(2147483648，与哨兵仅差 4096) 判成 unlimited，
    # 结果 OOM 保护反而失效——两个方向的错都必须挡住。
    if (abs(total_b - _CGROUP_V1_UNLIMITED_32BIT) <= _CGROUP_V1_UNLIMITED_TOLERANCE
            and (total_b & (total_b - 1)) != 0):
        logger.debug(
            "[ResourceGuard] cgroup v1 上限 {} B 命中 32 位 unlimited 哨兵，视为无限制",
            total_b,
        )
        return None

    # 补充交叉校验：容器上限不可能 >= 宿主总内存（覆盖其它位宽的哨兵形态）。
    host = _mem_from_proc()
    if host is not None and total_b >= host[0]:
        return None

    avail_b = max(0, total_b - used_b)

    # working_set 修正：inactive_file 是可回收页缓存，从占用里剔除
    stat_txt = _read_text(f"{base}/memory.stat")
    if stat_txt:
        inactive_file = None
        for line in stat_txt.splitlines():
            if line.startswith("total_inactive_file "):
                inactive_file = line.split()[1]
                break
        if inactive_file is None:
            for line in stat_txt.splitlines():
                if line.startswith("inactive_file "):
                    inactive_file = line.split()[1]
                    break
        if inactive_file is not None:
            try:
                working_set = max(0, used_b - int(inactive_file))
                avail_b = min(avail_b, max(0, total_b - working_set))
            except ValueError:
                pass
    return total_b, avail_b


def _mem_from_cgroup() -> tuple[int, int] | None:
    """cgroup 内存（total, available）字节数；无上限/非容器时返回 None。

    先 v2 后 v1：v2 unified 层级下读 v1 路径必然为空，反之亦然，
    两条互斥，顺序只影响多读两次文件（2s 采样一次，可忽略）。
    """
    return _mem_from_cgroup_v2() or _mem_from_cgroup_v1()


def _mem_cgroup_version() -> str | None:
    """返回当前生效的 cgroup 版本（"v2" / "v1"）；都没有上限时返回 None。

    观测用：`/api/health` 的 `generation_load` 会带出 source，
    排查"OOM 保护为何失效"时一眼能看出走的是哪条分支。
    """
    if _mem_from_cgroup_v2() is not None:
        return "v2"
    if _mem_from_cgroup_v1() is not None:
        return "v1"
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
    cg_v2 = _mem_from_cgroup_v2()
    cg_v1 = _mem_from_cgroup_v1() if cg_v2 is None else None
    cg = cg_v2 or cg_v1
    if cg is not None:
        total, avail = cg
        source = "cgroup_v2" if cg_v2 is not None else "cgroup_v1"
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
      - CPU 窗口均值 ≥ 85% 且 1 分钟负载 > **逻辑核数** → 收紧一档（机器已经在喘）
      - 可用内存 < 15% → 压到 1（防 OOM Kill，那比慢更致命）

    ⚠️ load_1m 的**量纲是"可运行任务数"，而 Linux 的可运行队列按逻辑 CPU 统计**
    （超线程的两个硬件线程各算一个 CPU）。故比较基准必须是 `cpu_limit()`
    （逻辑核/配额份额），**不是** `physical_cores()`：后者在 8 逻辑核机器上是 4，
    正常负载 4.5 就被判成"过载"，与 cpu_percent 89% 叠加后会把预算无故砍一档
    （实测：8 核机上 3 个生成任务被砍成 2，用户体感"明明是 8 核却只能跑 2 个"）。

    Args:
        snap: 采样快照；None 时现场取（采样线程未启动时的兜底）
    """
    base = min(inference_slots(), int(_cfg("GEN_TASK_CONCURRENCY_MAX", GEN_TASK_CONCURRENCY_MAX)))
    base = max(1, int(base))
    # ---- 兄弟容器投影：只压"并发任务数"，且"留 1 核"不重复扣 ----
    #
    # 为什么投影放在这里而不是 `inference_slots()`：那个函数还被
    # `clamp_fp_concurrency()` 消费（单任务内的 FP 并行度），而 celery 是**异步
    # 旁路、大部分时间空闲**，用它永久砍掉 api 的单任务并行度是常态性浪费。
    # 详见 `inference_slots()` 的 docstring。
    #
    # ⚠️ 单位必须统一（v4.1.6 的 bug 就在这）：`peer` 是**逻辑核**，
    #    `physical_cores()` 是**物理核**，直接相减等于两把尺子混用。
    #    必须先把 peer 折算成物理核（向下取整：只有"至少占满"的整核才算真被拿走），
    #    再与自己的物理核份额相减。
    #
    # ⚠️ 这里**不要再 `-1`**（v4.1.6 的第二个 bug）：`inference_slots()` 内部
    #    已经留过 1 核给主进程，这里再减一次就是同一个理由扣两遍。
    #    实测 6 逻辑核机器：可用物理 2 核 → 再减 1 → ceiling=1（本应 2）。
    try:
        peer_lc, _peer_src = _read_peer_cpu_budget()
        if peer_lc:
            own_phys = physical_cores()
            peer_phys = physical_cores_claimed(peer_lc)
            eff_phys = max(1, own_phys - peer_phys)
            base = min(base, eff_phys)
    except Exception:  # noqa: BLE001 - 投影计算出错保持原值（fail-open）
        pass
    try:
        if snap is None:
            snap = _sampler.snapshot()
        mem = snap.get("memory") or {}
        ratio = mem.get("available_ratio")
        # ⚠️ 顺序（v4.1.5）：先判"CPU 打满"再判 OOM。
        #   两条保护**都成立**时该报哪一个？先判内存 → 日志/仪表盘只显示
        #   "按内存压到 1"，现场看不到"CPU 也打满了"，两次故障长得一模一样。
        #   先判 CPU 会把"内存告急（比值也 <1 但没低于阈值）"漏掉吗？不会——
        #   CPU 分支只降一档、不 return，内存分支随后仍会执行到。
        # 比较基准 = 逻辑核（与 load 同量纲）；取整后至少 1
        logical = max(1, int(cpu_limit()))
        if (snap.get("cpu_percent_avg") or 0) >= float(_cfg("CPU_OVERLOAD_PERCENT", CPU_OVERLOAD_PERCENT)) \
                and (snap.get("load_1m") or 0) > logical:
            base = max(1, base - 1)
        if ratio is not None and ratio < float(_cfg("MEM_LOW_RATIO", MEM_LOW_RATIO)):
            return 1
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
                # threads_per_core：超线程比（2 = 开了超线程，1 = 没开）。前端用它
                # 解释"为什么 6 逻辑核只给 3 个推理份额"。
                "threads_per_core": _threads_per_core(),
                "topology_source": _topology_source(),
                "encode_threads": encode_threads(),
                # 兄弟容器投影：非空说明本进程的并发预算已被"同机另一个容器"扣减过。
                # 空 = 单容器部署或不需干预（单进程就能吃满整机的形态）。
                "peer_cpu_budget": _read_peer_cpu_budget()[0] or None,
                "effective_logical_cores": effective_cpu_limit(),
                "peer_budget_source": _read_peer_cpu_budget()[1],
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
        """极轻量字段（纯内存计数，不读 /proc），可随 /api/health 每次探活返回。

        采样线程未启动时（独立进程/测试/启动早期）快照为空，
        此时现场补一次内存探测——否则 mem_source 会是 "unknown"，
        部署排查时无法判断 OOM 保护有没有依据。内存探测只读 2-4 个小文件，
        且在 health_brief 里最多发生一次，不会成为阻塞源。
        """
        mem = _sampler.snapshot().get("memory") or {}
        if not mem or mem.get("source") in (None, "", "unknown"):
            try:
                mem = _mem_snapshot_raw()
            except Exception:  # noqa: BLE001 - 观测字段失败不能拖垮探活
                mem = {}
        return {
            "cpu_logical_cores": int(cpu_limit()),
            "cpu_physical_cores": physical_cores(),
            # cpu_topology：physical_cores 是"测"出来的还是"÷2 猜"出来的。
            # cpuinfo = 读了 /proc/cpuinfo 的 siblings / cpu cores（真拓扑）；
            # heuristic = 读不到拓扑，退回逻辑核÷2（保守值，宁少不多）。
            "cpu_topology": _topology_source(),
            "cpu_threads_per_core": _threads_per_core(),
            "encode_threads": encode_threads(),
            "inference_slots": inference_slots(),
            "task_ceiling": self.task_ceiling(),
            "active_tasks": _gate.active_tasks,
            "waiting_tasks": _gate.waiting_count,
            # v4.1.5：OOM 保护的"有效性证据"。source=proc 意味着读的是宿主/VM 内存，
            # 不是容器自己的上限 —— 那时 MEM_LOW_RATIO 判定不可信，需在部署侧给容器
            # 显式 mem_limit（deploy.sh 会就此告警）。3.10 上 v1 分支未覆盖时这里会显示 proc。
            "mem_source": mem.get("source", "unknown"),
            "mem_available_ratio": mem.get("available_ratio"),
            # cpu_source：回答"并发上限为什么是这个数"。
            # cgroup_v2/cgroup_v1 = 读到容器配额；affinity = 只有核亲和性；
            # cpuset = 兜底路径（affinity 不可用）；cpu_count = **未约束，看得到宿主全部核**，
            # 此时 task_ceiling 可能按宿主机核数放大 —— 需在部署侧给 --cpus 或 cpuset。
            "cpu_source": _cpu_snapshot_source(),
            # 兄弟容器投影（v4.1.5）：非空表示并发预算已按"同机另一个容器"的份额扣减。
            # 部署脚本会写进来；空值 = 单容器形态，预算只看自己。
            "cpu_peer_budget": (_read_peer_cpu_budget()[0] or None),
            "cpu_peer_budget_source": _read_peer_cpu_budget()[1] or None,
            "cpu_effective_logical_cores": effective_cpu_limit(),
        }


resource_guard = _ResourceGuard()
