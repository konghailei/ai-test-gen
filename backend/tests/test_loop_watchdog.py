"""事件循环卡顿看门狗行为单测（v4.0.1 / S1 从根治理）。

守卫目标（验**行为**而非源码字面量）：
  1. 事件循环被同步阻塞超过 error 阈值 → 累计 error 计数、记录最大卡顿时长、
     并留下**栈快照时间戳**（这才是"能定位阻塞点"的凭据）；
  2. 未阻塞时不误报（避免噪声淹没真问题）；
  3. 开关可关闭；stop() 能干净收尾。

实现方式：在 async 测试里用 `time.sleep()` **真实阻塞事件循环**，
再断言看门狗把这次阻塞记录了下来——不依赖任何 mock 的"假装阻塞"。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.config import settings
from app.core.loop_watchdog import LoopLagWatchdog


@pytest.fixture
def fast_thresholds(monkeypatch):
    """把阈值压到毫秒级，让测试在百毫秒内跑完。"""
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_WARN_S", 0.05)
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_ERROR_S", 0.15)
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_DUMP_ON_WARN", True)
    return None


@pytest.mark.asyncio
async def test_records_blocking_and_dumps_stack(fast_thresholds):
    wd = LoopLagWatchdog()
    wd.SAMPLE_INTERVAL = 0.01  # 加快采样，缩短测试时长
    wd.start()
    try:
        await asyncio.sleep(0.03)          # 正常调度，不应产生告警
        assert wd.stats()["lag_error_count"] == 0

        time.sleep(0.3)                    # ← 真实阻塞事件循环 300ms
        await asyncio.sleep(0.05)          # 让看门狗恢复采样并记账

        st = wd.stats()
        assert st["enabled"] is True
        assert st["lag_error_count"] >= 1, "超过 error 阈值的阻塞必须被记录"
        assert st["max_lag_ms"] >= 200, f"最大卡顿时长应≥200ms，实际 {st['max_lag_ms']}"
        assert st["last_stack_dump_at"], "发生 error 级阻塞时必须留下栈快照时间戳"
        assert st["last_lag_at"], "必须记录最近卡顿时间"
    finally:
        await wd.stop()
    assert wd.stats()["enabled"] is False


@pytest.mark.asyncio
async def test_no_false_positive_when_idle(fast_thresholds):
    """空闲（每 10ms 醒来一次）不应触发任何告警——否则噪声会淹没真问题。"""
    wd = LoopLagWatchdog()
    wd.SAMPLE_INTERVAL = 0.01
    wd.start()
    try:
        await asyncio.sleep(0.2)
        st = wd.stats()
        assert st["lag_error_count"] == 0
        assert st["lag_warn_count"] == 0, f"空闲出现误报，warn={st['lag_warn_count']}"
    finally:
        await wd.stop()


@pytest.mark.asyncio
async def test_disabled_by_setting(monkeypatch):
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_ENABLED", False)
    wd = LoopLagWatchdog()
    wd.start()
    assert wd.stats()["enabled"] is False, "关闭后不得启动采样任务"
    time.sleep(0.2)  # 即便阻塞也不应有任何记账
    assert wd.stats()["lag_error_count"] == 0
    await wd.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent(fast_thresholds):
    wd = LoopLagWatchdog()
    wd.SAMPLE_INTERVAL = 0.01
    wd.start()
    await wd.stop()
    await wd.stop()  # 重复 stop 不应抛错
    assert wd.stats()["enabled"] is False


# ---------------------------------------------------------------------------
# 采样线程通道（v4.0.1 追加）：事后线程栈看不到"循环被占住时在执行什么"，
# 必须由独立采样线程在阻塞**期间**留存现场。
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sampler_records_samples_and_window(fast_thresholds):
    """卡顿窗口摘要必须来自采样线程留存的样本，而非事后线程栈。"""
    wd = LoopLagWatchdog()
    wd.SAMPLE_INTERVAL = 0.01
    wd.start()
    try:
        await asyncio.sleep(0.08)
        assert len(wd._hot) > 0, "采样线程应已积累样本（否则循环被占住时无现场可取）"

        time.sleep(0.3)          # 真实阻塞；阻塞期间由采样线程留存 MainThread 栈
        await asyncio.sleep(0.06)

        focus = wd._last_dump_focus
        assert "窗口内样本" in focus, f"应产出阻塞窗口摘要，实际={focus[:200]}"
        assert wd.stats()["last_stack_dump_at"], "应记录快照时间"
    finally:
        await wd.stop()


def test_idle_thread_filter_keeps_our_code():
    """空闲线程必须被判空闲（否则 100+ 个池线程会把 dump 刷满、挤掉主线程）。"""
    from app.core.loop_watchdog import _APP_PREFIX, _is_idle_stack

    idle = (
        '  File "/usr/local/lib/python3.12/concurrent/futures/thread.py", line 90, in _worker\n'
        "    work_item = work_queue.get(block=True)\n"
    )
    assert _is_idle_stack(idle) is True

    ours = (
        f'  File "{_APP_PREFIX}/agents/gen_agent.py", line 1, in generate\n'
        "    do_work()\n"
    )
    assert _is_idle_stack(ours) is False, "含本项目代码的线程不得被判为空闲"

    active = (
        '  File "/usr/local/lib/python3.12/site-packages/torch/nn/modules/module.py", line 1, in forward\n'
        "    return self._call_impl(*args, **kwargs)\n"
    )
    assert _is_idle_stack(active) is False, "正在计算的线程不得被判为空闲"


@pytest.mark.asyncio
async def test_disabled_does_not_start_sampler(monkeypatch):
    monkeypatch.setattr(settings, "LOOP_WATCHDOG_ENABLED", False)
    wd = LoopLagWatchdog()
    wd.start()
    assert wd._sampler is None, "关闭时不应启动采样线程"
    assert wd.stats()["enabled"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
