"""事件循环卡顿看门狗（v4.0.1 / S1 从根治理）。

## 为什么需要它

S1（生成期间 `/api/health`、`/auth/login`、任务 cancel **全部超时**）本轮最初的定位方式是
"靠端点超时这一**间接现象**倒推"：先排除 LLM 与 SSE（皆为异步），再用静态扫描找未隔离的同步调用。

这种方式的根本缺陷是：**它只能发现"你想到要找"的阻塞源**。
实测 20s 量级超时无法用已定位的单点（约 2s）解释（v4.0.1 复跑仍测得 1-4s 卡顿），
说明还有未暴露的阻塞源。故补上运行时观测。

## 两个采样通道（缺一不可）

1. **事后线程栈**（`_thread_snapshot`）——卡顿发生时 dump 所有线程栈。
   适用：阻塞源是"某个线程里长时间执行的 Python 代码"。
   ⚠️ 局限（实测踩坑）：若阻塞源是**事件循环自己**被占住，等我们发现时栈已经展开，
   主线程栈上只剩看门狗自身——**事后采样天生看不到现场**。

2. **独立采样线程 + 环形缓冲**（`_sampler_loop`）——后台线程以 ~50ms 频率
   留存**事件循环所在线程(MainThread)** 的栈到环形缓冲；卡顿发生时，
   把"阻塞窗口内"的样本按时间顺序打出来。
   这才能回答"**循环被占住的那几秒，它到底在执行什么代码**"。
   采样线程本身是 daemon 且开销极低（20Hz × 6 帧）。

3. 另附**挂起协程摘要**（`_pending_coroutines_summary`）：列出仍挂起且栈顶位于本项目
   代码的 `asyncio.task`，用于判断"卡顿发生在哪一步业务逻辑"。

## 开销

- 循环侧：每 100ms 一次 `loop.time()` 差值比较；
- 采样线程：20Hz 取栈（仅 6 帧），常驻但可忽略；
- 栈转储：仅在超阈值时发生，且有冷却窗口（默认 10s）与线程数上限；
- `LOOP_WATCHDOG_ENABLED=false` 可整体关闭。

## 指标

`stats()` 输出累计 warn/error 次数、最大与最近卡顿时长，由 `/api/health`
透出（`data.event_loop`），使"生成期是否仍在阻塞"变成**可测量**的而非靠感觉。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logger import logger

# 事后线程栈：每线程保留帧数 / 最多展开的线程块数
_STACK_FRAMES = 8
_MAX_THREADS = 20
# 采样线程：采样间隔（秒）与环形缓冲容量（默认覆盖最近 ~2s）
_SAMPLER_INTERVAL = 0.05
_SAMPLER_BUFFER = 40
# 采样栈的帧数（只看栈顶，够判断"在跑什么"）
_SAMPLER_FRAMES = 6

# "本项目代码" 的路径前缀：用于优先挑选与我们代码有关的线程
_APP_PREFIX = str(Path(__file__).resolve().parents[1])


def _is_idle_stack(text: str) -> bool:
    """判断线程是否"明显空闲"（等活儿），用于过滤噪声线程。

    首版没做这个过滤，结果 100+ 个空闲线程池 worker 把 dump 刷满、
    **把主线程挤出了截断窗口**（实测踩坑）——这些线程不可能是阻塞源。
    """
    if _APP_PREFIX in text:
        return False  # 栈上有我们的代码 → 不空闲
    if "work_queue.get(block=True)" in text:
        return True
    tail = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not tail:
        return True
    last = tail[-1]
    if "Queue.get(" in last:
        return True
    return "threading.py" in last and ("wait(" in last or "_wait(" in last or "acquire(" in last)


def _collapse(samples: list[tuple[float, str, bool]]) -> str:
    """把连续相同的采样栈折叠成 `×N`，便于一眼看出"卡住时反复在执行什么"。"""
    lines: list[str] = []
    prev = None
    repeat = 0
    for ts, stack, is_app in samples:
        if prev is not None and stack == prev:
            repeat += 1
            continue
        if prev is not None:
            lines.append(f"    （上一段重复 {repeat} 次）" if repeat > 1 else "")
            lines.append(prev)
        prev = stack
        repeat = 1
    if prev is not None:
        lines.append(f"    （重复 {repeat} 次）" if repeat > 1 else "")
        lines.append(prev)
    return "\n".join(ln for ln in lines if ln)


class LoopLagWatchdog:
    """事件循环延迟监控（进程级单例）。"""

    SAMPLE_INTERVAL = 0.1  # 循环侧采样间隔（秒）

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._sampler: threading.Thread | None = None
        self._stopped = True

        self._warn_s = 0.5
        self._error_s = 3.0
        self._dump_on_warn = True
        self._cooldown_s = 10.0

        self._last_dump_ts = 0.0
        self._warn_count = 0
        self._error_count = 0
        self._max_lag_ms = 0.0
        self._last_lag_ms = 0.0
        self._last_lag_at = ""
        self._last_blocked_at = ""
        self._last_dump_focus = ""
        self._started_at = ""
        # 环形缓冲：(单调时间戳, 折叠后的栈文本, 是否含本项目代码)
        self._hot: deque[tuple[float, str, bool]] = deque(maxlen=_SAMPLER_BUFFER)

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动监控（lifespan 中调用；重复调用幂等）。"""
        try:
            from app.config import settings

            if not bool(getattr(settings, "LOOP_WATCHDOG_ENABLED", True)):
                logger.info("[LoopWatchdog] 已禁用（LOOP_WATCHDOG_ENABLED=false）")
                return
            self._warn_s = float(getattr(settings, "LOOP_WATCHDOG_WARN_S", 0.5) or 0.5)
            self._error_s = float(getattr(settings, "LOOP_WATCHDOG_ERROR_S", 3.0) or 3.0)
            self._dump_on_warn = bool(getattr(settings, "LOOP_WATCHDOG_DUMP_ON_WARN", True))
        except Exception as e:  # noqa: BLE001 - 配置读取失败不阻断启动
            logger.warning("[LoopWatchdog] 配置读取失败，使用默认阈值: {}", str(e)[:120])

        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._started_at = datetime.now(timezone.utc).isoformat()

        # ① 采样线程（daemon，独立于事件循环——循环被占住时它仍能记录现场）
        self._sampler = threading.Thread(
            target=self._sampler_loop, name="loop-lag-sampler", daemon=True,
        )
        self._sampler.start()
        # ② 循环侧计时任务
        self._task = asyncio.create_task(self._run(), name="loop-lag-watchdog")
        logger.info(
            "[LoopWatchdog] 事件循环卡顿监控已启动: 采样={}ms/{}ms, warn={}s, error={}s",
            int(self.SAMPLE_INTERVAL * 1000), int(_SAMPLER_INTERVAL * 1000),
            self._warn_s, self._error_s,
        )

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - 取消/退出异常均忽略
                pass
        # 采样线程是 daemon，靠 _stopped 自然退出（不 join，避免关闭期卡住）

    # ------------------------------------------------------------------ 采样线程

    def _sampler_loop(self) -> None:
        """后台线程：高频留存事件循环所在线程的栈（卡顿现场的唯一取证手段）。"""
        main_tid = threading.main_thread().ident
        frames = sys._current_frames
        while not self._stopped:
            try:
                frame = frames().get(main_tid)
                if frame is not None:
                    stack = "".join(traceback.format_stack(frame, limit=_SAMPLER_FRAMES))
                    self._hot.append((time.monotonic(), stack, _APP_PREFIX in stack))
            except Exception:  # noqa: BLE001 - 采样失败绝不影响业务
                pass
            time.sleep(_SAMPLER_INTERVAL)

    # ------------------------------------------------------------------ 循环侧计时

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.SAMPLE_INTERVAL
        # expected 是"下一次应当被唤醒的时刻"；实际唤醒晚于它的差值
        # 就是这一窗口内事件循环被占住的时长。
        expected = loop.time() + interval
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            now = loop.time()
            drift = now - expected
            expected = now + interval
            if drift <= 0:
                continue
            self._record(drift)

    def _record(self, drift_s: float) -> None:
        lag_ms = round(drift_s * 1000, 1)
        self._last_lag_ms = lag_ms
        if lag_ms > self._max_lag_ms:
            self._max_lag_ms = lag_ms

        now_iso = datetime.now(timezone.utc).isoformat()
        if drift_s >= self._error_s:
            self._error_count += 1
            self._last_lag_at = now_iso
            logger.error(
                "[LoopWatchdog] ⛔ 事件循环被阻塞 {} ms（超过 error 阈值 {}s）"
                "——生成期端点超时的直接成因即此类阻塞",
                lag_ms, self._error_s,
            )
            self._dump(drift_s, force=True)
        elif drift_s >= self._warn_s:
            self._warn_count += 1
            self._last_lag_at = now_iso
            logger.warning(
                "[LoopWatchdog] ⚠️ 事件循环卡顿 {} ms（超过 warn 阈值 {}s）",
                lag_ms, self._warn_s,
            )
            if self._dump_on_warn:
                self._dump(drift_s, force=False)

    # ------------------------------------------------------------------ 转储

    def _dump(self, lag_s: float, *, force: bool) -> None:
        now = time.monotonic()
        if not force and (now - self._last_dump_ts) < self._cooldown_s:
            return
        self._last_dump_ts = now
        self._last_blocked_at = datetime.now(timezone.utc).isoformat()

        try:
            focus = self._blocked_window(lag_s)
            self._last_dump_focus = focus
            logger.error(
                "[LoopWatchdog] 阻塞现场（窗口 {}ms）：\n"
                "  ── ① 阻塞窗口内事件循环在执行什么（采样线程 20Hz 留存，重复已折叠）──\n{}",
                int(lag_s * 1000), focus,
            )
            logger.error(
                "[LoopWatchdog] 阻塞现场（续）：\n"
                "  ── ② 挂起协程摘要（本项目代码）──\n{}\n"
                "  ── ③ 线程栈快照（已过滤空闲线程）──\n{}",
                self._pending_coroutines_summary(), self._thread_snapshot(),
            )
        except Exception as e:  # noqa: BLE001 - 采样失败绝不影响业务
            logger.warning("[LoopWatchdog] 栈采样失败（忽略）: {}", str(e)[:150])

    def _blocked_window(self, lag_s: float) -> str:
        """取"阻塞窗口"内的采样样本——这是定位阻塞点的**主证据**。"""
        if not self._hot:
            return "    （采样线程未启动或无样本）"
        cutoff = time.monotonic() - lag_s - 0.05
        samples = [s for s in list(self._hot) if s[0] >= cutoff]
        if not samples:
            return "    （窗口内无样本）"
        app_hits = sum(1 for _, _, is_app in samples if is_app)
        head = (f"    窗口内样本 {len(samples)} 个，其中栈顶含**本项目代码** {app_hits} 个；"
                f"最近 {len(samples)} 个样本如下：")
        return head + "\n" + _collapse(samples)

    def _thread_snapshot(self) -> str:
        """线程栈快照：主线程优先 → 含本项目代码的线程 → 其余非空闲线程。"""
        names = {t.ident: t.name for t in threading.enumerate()}
        main_tid = threading.main_thread().ident
        frames = sys._current_frames()

        main_part: list[list[str]] = []
        app_part: list[list[str]] = []
        other_part: list[list[str]] = []
        idle_count = 0

        for tid, frame in frames.items():
            tname = names.get(tid, f"thread-{tid}")
            stack = traceback.format_stack(frame, limit=_STACK_FRAMES)
            text = "".join(stack)
            block = [f"  ── 线程 {tname} (tid={tid}) ──"]
            block += ["    " + ln.rstrip() for ln in stack]
            if tid == main_tid:
                main_part.append(block)
            elif _APP_PREFIX in text:
                app_part.append(block)
            elif _is_idle_stack(text):
                idle_count += 1
            else:
                other_part.append(block)

        blocks = main_part + app_part + other_part
        shown = blocks[:_MAX_THREADS]
        truncated = len(blocks) - len(shown)
        body = "\n".join(ln for blk in shown for ln in blk)
        if truncated > 0:
            body += f"\n  ... 其余 {truncated} 个线程已省略"
        body += f"\n  （空闲等活儿线程 {idle_count} 个未展开）"
        return body

    @staticmethod
    def _pending_coroutines_summary(limit: int = 12) -> str:
        """打出仍挂起、且栈顶位于本项目代码的协程（判断卡顿发生在哪步业务）。"""
        try:
            lines: list[str] = []
            for task in asyncio.all_tasks():
                try:
                    stack = task.get_stack(limit=6)
                except Exception:  # noqa: BLE001
                    continue
                hit = None
                for fr in reversed(stack):
                    if _APP_PREFIX in fr.f_code.co_filename:
                        hit = fr
                        break
                if hit is None:
                    continue
                lines.append(
                    f"    {task.get_name()} @ "
                    f"{hit.f_code.co_filename.replace(_APP_PREFIX, 'app')}"
                    f":{hit.f_lineno} in {hit.f_code.co_name}"
                )
                if len(lines) >= limit:
                    lines.append("    ...")
                    break
            return "\n".join(lines) if lines else "    （无挂起中的本项目协程）"
        except Exception as e:  # noqa: BLE001
            return f"    （摘要失败: {str(e)[:80]}）"

    # ------------------------------------------------------------------ 指标

    def stats(self) -> dict:
        """返回监控指标（供 /api/health 透出）。"""
        return {
            "enabled": not self._stopped,
            "sampling_ms": int(self.SAMPLE_INTERVAL * 1000),
            "sampler_ms": int(_SAMPLER_INTERVAL * 1000),
            "warn_threshold_s": self._warn_s,
            "error_threshold_s": self._error_s,
            "lag_warn_count": self._warn_count,
            "lag_error_count": self._error_count,
            "max_lag_ms": self._max_lag_ms,
            "last_lag_ms": self._last_lag_ms,
            "last_lag_at": self._last_lag_at,
            "last_stack_dump_at": self._last_blocked_at,
            "started_at": self._started_at,
        }


# 进程级单例
loop_watchdog = LoopLagWatchdog()
