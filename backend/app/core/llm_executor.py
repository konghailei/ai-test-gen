"""LLM 专用执行线程（v4.0.1 / S1 根因修复）。

## 为什么需要

S1（生成期间 `/api/health`、`/auth/login`、任务 cancel 全部超时）的**根因**经
运行时看门狗取证为：**litellm 的 `StreamingHandler` 在事件循环所在线程里做纯 Python 流处理**——

- 每个 chunk：字符串累加 `self.response_uptil_now += ...` + `self.rules.post_call_rules(...)`；
- 流结束时（`except StopAsyncIteration` 分支）：**无条件**调用
  `litellm.stream_chunk_builder(chunks=self.chunks, messages=self.messages)`
  组装"完整响应"（仅为写日志/取 usage）。思考模式下 chunk 数可达数万，
  该函数逐 chunk 做 pydantic 属性访问。

实测证据（v4.0.1 看门狗"阻塞窗口"采样，docs/TEST_PLAN.md §K.1c）：阻塞窗口内事件循环
**100% 的样本**都停在这条链上 ——
`llm_client._call_with_retry:1283 → asyncio.wait_for → litellm StreamingHandler.__anext__
→ litellm.main.stream_chunk_builder → .../streaming_chunk_builder_utils.py`，
单次占住事件循环 **1.5-6s**，且每个 LLM 调用都会发生一次。

关键点：该开销发生在 `await gen.__anext__()` **内部**，所以"在业务代码里加
`asyncio.to_thread`"根本无法隔离——只能把"**消费整条流**"这件事整体搬到主循环之外。

## 做法

一条常驻后台线程 + 该线程内常驻事件循环；`llm_client` 把**每次尝试**
（`_call_with_retry`，含取流、逐 chunk 消费、重试退避、非流式回退）整段提交到该线程执行，
API 侧只 `await wrap_future(run_coroutine_threadsafe(...))`。

**为什么"常驻"而不是每次 `asyncio.run`**：litellm / openai 的 `AsyncClient` 会缓存
绑定到首个事件循环的连接池；反复新建并关闭事件循环会触发
"Event loop is closed" / "attached to a different loop"。常驻单 loop 既稳定，
又让第三方库的所有 CPU 开销**天然落在该线程**，与 API 服务互不干扰。

**代价（可接受）**：多个并发 LLM 流的 CPU 段在该线程上串行化——只影响"LLM 结果返回
稍晚"，不影响 HTTP 端点的响应性，这正是我们要的取舍。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

from app.utils.logger import logger

T = TypeVar("T")

_START_TIMEOUT = 10.0


class LlmExecutor:
    """常驻后台线程 + 常驻事件循环，承载所有 LLM 调用。"""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started_at: str = ""

    # ------------------------------------------------------------------ 生命周期

    def ensure_started(self) -> bool:
        """确保执行线程已就绪（幂等）。返回是否可用。"""
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._loop is not None
                and not self._loop.is_closed()
            ):
                return True
            ready = threading.Event()

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for t in pending:
                            t.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                    except Exception:  # noqa: BLE001 - 收尾失败不影响进程退出
                        pass
                    loop.close()
                    self._loop = None
                    logger.info("[LlmExecutor] 执行线程已退出")

            self._thread = threading.Thread(
                target=_runner, name="llm-executor", daemon=True
            )
            self._thread.start()
            if not ready.wait(_START_TIMEOUT):
                logger.error("[LlmExecutor] 启动超时（{}s），LLM 调用将退化为同线程执行", _START_TIMEOUT)
                return False
            import datetime as _dt

            self._started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
            logger.info("[LlmExecutor] LLM 执行线程已就绪（isolated event loop）")
            return True

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """在 LLM 执行线程上执行协程并等待结果（失败时自动退化为本线程执行）。

        退化路径的意义：执行器不可用不应让 LLM 功能整体不可用——
        宁可退回"可能阻塞主循环"的旧行为，也不能直接失败。
        """
        if not self.ensure_started() or self._loop is None:
            return await coro
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as e:  # noqa: BLE001
            logger.warning("[LlmExecutor] 提交失败（退化为同线程执行）: {}", str(e)[:150])
            return await coro
        return await asyncio.wrap_future(fut)

    def shutdown(self) -> None:
        """停止执行线程（应用关闭时调用）。"""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    # ------------------------------------------------------------------ 观测

    def stats(self) -> dict:
        return {
            "enabled": bool(self._thread and self._thread.is_alive()),
            "isolated_loop": bool(self._loop and not self._loop.is_closed()),
            "started_at": self._started_at,
        }


llm_executor = LlmExecutor()
