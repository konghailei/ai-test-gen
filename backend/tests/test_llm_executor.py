"""LLM 专用执行线程行为单测（v4.0.1 / S1 根因修复）。

根因（看门狗取证，docs/TEST_PLAN.md §K.1c）：litellm 的 `StreamingHandler` 在
**事件循环所在线程**做纯 Python 流处理，流结束时还无条件 `stream_chunk_builder`
遍历全部 chunk 组装完整响应（思考模式下数万 chunk），实测占住循环 1.5-6s。
该开销在 `await gen.__anext__()` 内部，无法用 to_thread 单点隔离。

因此把"消费整条流"整体搬到专用执行线程。本测试验证**该隔离真的成立**：
  1. 协程在**另一个线程**、**另一个事件循环**上执行；
  2. 执行线程内的同步 CPU 阻塞**不会**卡住调用方事件循环（S1 的核心诉求）；
  3. 返回值与异常都能正确回传；
  4. `shutdown()` 能干净收尾。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.core.llm_executor import LlmExecutor


@pytest.mark.asyncio
async def test_runs_on_separate_thread_and_loop():
    ex = LlmExecutor()
    try:
        caller_loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()

        async def probe():
            return threading.get_ident(), asyncio.get_running_loop()

        tid, loop = await ex.run(probe())
        assert tid != main_thread, "LLM 调用必须在独立线程执行"
        assert loop is not caller_loop, "必须使用独立事件循环（第三方客户端需稳定 loop）"
    finally:
        ex.shutdown()


@pytest.mark.asyncio
async def test_result_and_exception_propagate():
    ex = LlmExecutor()
    try:
        async def ok():
            return {"a": 1}

        assert await ex.run(ok()) == {"a": 1}

        async def boom():
            raise ValueError("provider exploded")

        with pytest.raises(ValueError, match="provider exploded"):
            await ex.run(boom())
    finally:
        ex.shutdown()


@pytest.mark.asyncio
async def test_sync_cpu_in_executor_does_not_block_caller_loop():
    """S1 的核心诉求：执行线程里的同步 CPU 阻塞不得影响调用方事件循环。"""
    ex = LlmExecutor()
    ticks = 0

    async def heavy():
        # 模拟 litellm 流结束时的 stream_chunk_builder（纯 Python 同步 CPU）
        time.sleep(0.4)
        return "done"

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        assert await ex.run(heavy()) == "done"
    finally:
        t.cancel()
        ex.shutdown()
    # 0.4s 内每 20ms 一跳，若调用方循环未被阻塞应远多于 5 次
    assert ticks >= 5, f"调用方事件循环被阻塞了（ticks={ticks}）"


@pytest.mark.asyncio
async def test_concurrent_runs_are_multiplexed():
    """并发提交应真正重叠执行（不是串行等待）。"""
    ex = LlmExecutor()
    try:
        async def sleepy():
            await asyncio.sleep(0.2)
            return 1

        t0 = time.perf_counter()
        results = await asyncio.gather(*(ex.run(sleepy()) for _ in range(4)))
        elapsed = time.perf_counter() - t0
        assert results == [1, 1, 1, 1]
        assert elapsed < 0.6, f"并发未重叠（耗时 {elapsed:.2f}s）"
    finally:
        ex.shutdown()


def test_shutdown_is_safe_and_idempotent():
    ex = LlmExecutor()
    ex.ensure_started()
    assert ex.stats()["enabled"] is True
    ex.shutdown()
    ex.shutdown()  # 重复关闭不应抛错
    assert ex.stats()["enabled"] is False


def test_fallback_when_executor_unavailable(monkeypatch):
    """执行器不可用时必须退化为同线程执行——LLM 功能不能因隔离层故障而整体不可用。"""
    ex = LlmExecutor()
    monkeypatch.setattr(ex, "ensure_started", lambda: False)

    async def coro():
        return "in-loop"

    async def main():
        return await ex.run(coro())

    assert asyncio.run(main()) == "in-loop"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
