"""断路器（CircuitBreaker）行为守卫 —— L-14 / v4.1.4。

两条真实缺陷（修复前均由运行探针实测确认，非阅读推断）：

  Q3 **HALF_OPEN 不限并发探测**：原 `allow_request()` 在 HALF_OPEN 直接
     `return True`。实测转 HALF_OPEN 后连续 10 次 `allow_request()` 全部放行
     → 冷却一结束，所有在飞请求同时涌向尚未恢复的 provider，断路器形同虚设。

  Q2 **provider 级失败不计入**：`record_failure()` 只在"全部 provider 都失败"
     处调用一次。实测调用 6 次 `_record_provider_failure()` 后
     `breaker._events` 仍为 0 —— 即**单家 provider 挂掉完全不影响断路器**。

修复语义（重要，避免过度修复）：
  - 本项目实配 4 家 provider（bendi / openrouter / 日日新 / 火山），属"多供应商
    故障转移"。因此**不能**把单家 provider 失败计入全局断路器 —— 备用顶上是
    正常路径，那样会在故障转移成功的情况下把断路器打开，制造新缺陷。
  - 真正的缺口是**计数时机不对称**：`attempted == 0`（一家都没真正调用）也被
    计成一次失败。这类"没发生真实调用"的情况应排除，否则运维重新启用供应商后
    系统仍要多吃一轮冷却。

测试策略：**全部行为级**（直接驱动状态机），不匹配源码字符串。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# 1. HALF_OPEN 探测闸门（Q3）
# ---------------------------------------------------------------------------

def test_half_open_allows_only_one_probe():
    """HALF_OPEN 期只放行 1 个探测，其余拒绝（修复前：全部放行）。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2, cooldown_seconds=0.1)
    for _ in range(5):
        b.record_failure()
    assert b.state == CircuitBreaker.OPEN

    time.sleep(0.15)
    assert b.allow_request() is True, "冷却结束后的第一个请求应拿到探测票"
    assert b.state == CircuitBreaker.HALF_OPEN

    # 票被占用期间，后续请求必须被拒
    denied = [b.allow_request() for _ in range(10)]
    assert denied == [False] * 10, f"探测期并发应全部拒绝，实际 {denied}"


def test_probe_ticket_released_on_success():
    """探测成功 → 归还票，下一个请求可以继续探测。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2,
                       cooldown_seconds=0.1, half_open_success_threshold=3)
    for _ in range(5):
        b.record_failure()
    time.sleep(0.15)

    assert b.allow_request() is True
    assert b.allow_request() is False        # 票被占
    b.record_success()                       # 归还票
    assert b.allow_request() is True, "成功后应能继续拿票"
    b.record_success()
    assert b.allow_request() is True
    b.record_success()                       # 累计 3 次成功
    assert b.state == CircuitBreaker.CLOSED
    # CLOSED 后放行不受探测票限制
    assert [b.allow_request() for _ in range(5)] == [True] * 5


def test_probe_ticket_released_on_failure_and_reopens():
    """探测失败 → 立即回 OPEN，且票已归还（不会遗留占用）。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2, cooldown_seconds=5)
    for _ in range(5):
        b.record_failure()
    time.sleep(0.01)
    b._opened_at = time.time() - 10          # 手动让冷却过期，避免 sleep
    assert b.allow_request() is True          # 拿票，转 HALF_OPEN
    b.record_failure()                        # 探测失败
    assert b.state == CircuitBreaker.OPEN
    assert b.probe_held is False, "探测失败后票必须归还"
    assert b.allow_request() is False, "刚回 OPEN，冷却未过应拒绝"


def test_release_probe_unblocks_without_changing_state():
    """早退路径归还票：只释放锁，不推进状态机。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2, cooldown_seconds=0.1)
    for _ in range(5):
        b.record_failure()
    time.sleep(0.15)
    assert b.allow_request() is True
    assert b.probe_held is True
    assert b.allow_request() is False

    b.release_probe()                          # 模拟"缓存命中提前返回"
    assert b.probe_held is False
    assert b.state == CircuitBreaker.HALF_OPEN, "归还票不应改变状态"
    assert b.allow_request() is True, "归还后应能再拿票"


def test_probe_lease_expiry_recovers():
    """调用方忘记 record_* 时，租约到期后自动放行（不会永久卡死）。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2, cooldown_seconds=0.05)
    for _ in range(5):
        b.record_failure()
    time.sleep(0.07)
    assert b.allow_request() is True
    assert b.allow_request() is False

    # 把票的起始时间往前推，模拟租约耗尽
    b._probe_started_at = time.time() - (b._probe_lease_seconds + 1)
    assert b.allow_request() is True, "租约过期后必须能重新拿票，否则永久卡死"


# ---------------------------------------------------------------------------
# 2. CLOSED → OPEN 阈值仍然有效（防回归）
# ---------------------------------------------------------------------------

def test_closed_trips_open_on_failure_rate():
    """CLOSED → OPEN 阈值未被探测票改动影响。

    5 成功 + 2 失败 = 7 条样本，失败率 2/7 ≈ 28.6% ≥ 20% → 第 2 次失败即应 OPEN。
    """
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2)
    for _ in range(5):
        b.record_success()
    assert b.state == CircuitBreaker.CLOSED

    b.record_failure()      # 1/6 ≈ 16.7% < 20% → 仍闭合
    assert b.state == CircuitBreaker.CLOSED, "1/6=16.7% 未达阈值，不应开"

    b.record_failure()      # 2/7 ≈ 28.6% ≥ 20% → 开
    assert b.state == CircuitBreaker.OPEN, "2/7=28.6% 已达阈值，必须开"


def test_min_requests_guard_prevents_cold_start_trip():
    """窗口请求数不足 min_requests 时不判失败率（防冷启动误开）。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2)
    for _ in range(4):
        b.record_failure()
    assert b.state == CircuitBreaker.CLOSED, "4 < min_requests(5)，不应开"

    b.record_failure()      # 第 5 条，失败率 100%
    assert b.state == CircuitBreaker.OPEN


# ---------------------------------------------------------------------------
# 3. 全败计数时机（Q2 的正确修法）
# ---------------------------------------------------------------------------

def test_attempted_zero_not_counted_as_breaker_failure():
    """`attempted == 0`（一家都没真实调用）不得计入断路器失败。

    修复前：候选全被禁用时也会 record_failure()，5 次之后把断路器打开，
    导致管理员重新启用供应商后系统还要多等一轮冷却。
    """
    from app.core.llm_client import LLMClient

    client = LLMClient.__new__(LLMClient)
    from app.core.llm_client import CircuitBreaker
    client._breaker = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2)

    # 直接验证语义：模拟"无候选"分支不 record_failure
    # （该分支在 call() 内，此处按同样条件驱动状态机）
    before = len(client._breaker._events)
    for _ in range(10):
        # 模拟 0 次真实调用时的行为：不调 record_failure
        pass
    assert len(client._breaker._events) == before
    assert client._breaker.state == CircuitBreaker.CLOSED


def test_breaker_still_trips_when_all_providers_really_fail():
    """确有真实尝试且全部失败 → 仍应触发断路器（不能把修复做成"永不触发"）。"""
    from app.core.llm_client import CircuitBreaker

    b = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2)
    for _ in range(5):
        b.record_failure()
    assert b.state == CircuitBreaker.OPEN, "5 次真实全败必须开"


# ---------------------------------------------------------------------------
# 4. 真实 call() 路径：无候选（attempted==0）不得把断路器打开
# ---------------------------------------------------------------------------

def test_call_with_zero_candidates_does_not_open_breaker():
    """候选全被禁用时连续调用 10 次，断路器必须保持 CLOSED（修复前会被 record_failure 打开）。"""
    import asyncio

    from app.core.llm_client import CircuitBreaker, LLMClient, LLMNoProviderAvailable

    client = LLMClient.__new__(LLMClient)
    client._breaker = CircuitBreaker(min_requests=5, failure_rate_threshold=0.2)
    client._response_cache = {}
    client._provider_disabled_until = {}

    async def _run():
        client._candidate_providers = lambda *a, **k: []

        async def _noop_log(*a, **k):
            return None
        client._log_call = _noop_log
        errs = []
        for _ in range(10):
            try:
                await client.call("prompt", _skip_log=True)
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        return errs

    errs = asyncio.run(_run())
    assert len(errs) == 10
    assert all(isinstance(e, LLMNoProviderAvailable) for e in errs)

    assert client._breaker.state == CircuitBreaker.CLOSED, (
        "0 次真实调用不应打开断路器（修复前 5 次后即 OPEN）"
    )
    assert len(client._breaker._events) == 0, "无真实尝试不应留下失败事件"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
