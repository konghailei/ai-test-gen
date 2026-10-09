# -*- coding: utf-8 -*-
"""v3.5.6 守卫：供应商禁用改造 + 等待重试（docs/23）。

行为验证（非 grep 字面量）：
1. 软失败不再触发跨调用禁用（_record_provider_failure 不再写内存禁用）；
2. 硬失败 → 持久化禁用（DB enabled=false + 原因 + 通知）+ 主标记让位；
3. 候选过滤：DB 禁用的供应商被排除；
4. 等待重试：全败后等待 N 次、回调被触发、最终抛错；
5. 手动启用/禁用语义（人工禁用原因固定）。
"""
import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app")

from app.core.llm_client import LLMClient, llm_client


# ============================================================================
# 组 1：候选过滤（DB 禁用）
# ============================================================================

def test_candidate_providers_skips_db_disabled():
    """DB 禁用的供应商必须从候选中排除。"""
    from app.config import settings
    old_cfgs = getattr(settings, "_all_provider_configs", None)
    old_disp = dict(llm_client._db_disabled_providers)
    settings._all_provider_configs = {
        "火山": {"api_key": "k1", "base_url": "u1", "model": "glm", "enable_thinking": False, "thinking_params": ""},
        "日日新": {"api_key": "k2", "base_url": "u2", "model": "sn", "enable_thinking": False, "thinking_params": ""},
        "openrouter": {"api_key": "k3", "base_url": "u3", "model": "or", "enable_thinking": False, "thinking_params": ""},
    }
    llm_client._db_disabled_providers = {"日日新": {"enabled": False, "reason": "403 额度耗尽"}}
    try:
        cands = llm_client._candidate_providers(None)
        keys = " ".join(c["key"] for c in cands)
        assert "日日新" not in keys, f"被禁用的供应商不应在候选中: {keys}"
        assert "火山" in keys and "openrouter" in keys
    finally:
        llm_client._db_disabled_providers = old_disp
        if old_cfgs is not None:
            settings._all_provider_configs = old_cfgs


# ============================================================================
# 组 2：软失败不禁用 / 硬失败持久化禁用
# ============================================================================

def test_soft_failure_no_cross_call_disable():
    """软失败（超时/重试耗尽）只记调用内计数，不写内存禁用、不写 DB。"""
    llm_client._provider_failures.clear()
    llm_client._provider_disabled_until.clear()
    prov = {"key": "火山:glm-5.3-flash", "name": "火山"}
    for _ in range(5):  # 超过旧阈值 3 次
        llm_client._record_provider_failure(prov)
    assert llm_client._provider_failures[prov["key"]] >= 5
    assert prov["key"] not in llm_client._provider_disabled_until, \
        "软失败不应再写内存禁用（v3.5.6 回归）"


class _FakeRow:
    """模拟 llm_config ORM 行（用于 provider_admin 的 fake session）。"""

    def __init__(self, provider, api_key="k", is_primary=False, enabled=True):
        self.provider = provider
        self.provider_label = provider
        self.api_key = api_key
        self.base_url = "u"
        self.model = "m"
        self.is_primary = is_primary
        self.enable_thinking = False
        self.thinking_params = ""
        self.enabled = enabled
        self.disabled_reason = ""
        self.disabled_by = ""
        self.disabled_at = None


class _FakeSession:
    """行为级 fake session（不连真 PG）。

    避免 unit 层依赖外部服务：真 DB 集成测试在 function-scoped event loop 下
    会因全局引擎连接池绑定旧 loop 报 `Event loop is closed`。

    路由策略（按 SQL 语义）：
    - 查询含 `ORDER BY llm_config.id` 且无 `provider !=` → 目标行（单行）
    - 查询含 `provider !=`（让位候选）→ other_rows（多行）
    """

    def __init__(self, target: _FakeRow | None, others: list[_FakeRow] | None = None):
        self._target = target
        self._others = others or []
        self.added: list = []
        self.committed = False

    async def execute(self, q):
        sql = str(q)
        rows = self._others if "!=" in sql else ([self._target] if self._target else [])

        class _R:
            def __init__(self, rows):
                self._rows = rows
            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None
            def scalars(self):
                return self
            def all(self):
                return self._rows
        return _R(rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_hard_failure_persists_disable_and_notifies():
    """硬失败 → 持久化禁用（enabled=false + 原因 + 通知）。"""
    from app.core import provider_admin as pa

    row = _FakeRow("测试供应商A")
    fake = _FakeSession(row)

    with patch.object(pa, "async_session", return_value=fake), \
         patch("app.api.settings.load_db_config_to_settings", AsyncMock()):
        await pa.disable_provider_persistently("测试供应商A", "403 额度耗尽")

    assert row.enabled is False, "硬失败后 enabled 应为 False"
    assert "403" in row.disabled_reason, f"原因应含硬失败原因: {row.disabled_reason}"
    assert row.disabled_by == "system"
    assert row.disabled_at is not None
    # 通知已写入
    notifs = [o for o in fake.added if type(o).__name__ == "AdminNotification"]
    assert notifs, "应写入管理员通知"
    assert "禁用" in notifs[0].title
    # 内存缓存已同步
    assert llm_client._db_disabled_providers.get("测试供应商A", {}).get("enabled") is False
    llm_client._db_disabled_providers.pop("测试供应商A", None)


@pytest.mark.asyncio
async def test_manual_disable_reason_fixed():
    """手动禁用 → disabled_by='manual'、reason 固定'人工禁用'；再启用清空。"""
    from app.core import provider_admin as pa

    row = _FakeRow("测试供应商B")
    fake = _FakeSession(row)
    with patch.object(pa, "async_session", return_value=fake), \
         patch("app.api.settings.load_db_config_to_settings", AsyncMock()):
        assert await pa.set_provider_enabled("测试供应商B", False) is True
        assert row.enabled is False and row.disabled_reason == "人工禁用"
        assert row.disabled_by == "manual" and row.disabled_at is not None
        # 再启用（有 api_key → 允许）
        assert await pa.set_provider_enabled("测试供应商B", True) is True
        assert row.enabled is True and row.disabled_reason == ""
        assert row.disabled_by == "" and row.disabled_at is None


@pytest.mark.asyncio
async def test_manual_enable_rejected_without_key():
    """无 api_key 的供应商不允许被启用（避免启用后必然失败）。"""
    from app.core import provider_admin as pa
    row = _FakeRow("测试供应商C", api_key="")
    fake = _FakeSession(row)
    with patch.object(pa, "async_session", return_value=fake), \
         patch("app.api.settings.load_db_config_to_settings", AsyncMock()):
        assert await pa.set_provider_enabled("测试供应商C", True) is False


@pytest.mark.asyncio
async def test_primary_reassigned_on_disable():
    """主供应商被禁用 → 主标记让给另一个启用中的供应商。"""
    from app.core import provider_admin as pa

    main = _FakeRow("测试主供应商Z", is_primary=True)
    backup = _FakeRow("测试备供应商Y", api_key="k2", is_primary=False)
    fake = _FakeSession(target=main, others=[backup])

    with patch.object(pa, "async_session", return_value=fake), \
         patch("app.api.settings.load_db_config_to_settings", AsyncMock()):
        await pa.disable_provider_persistently("测试主供应商Z", "401 认证失败")

    assert main.is_primary is False, "被禁用的主供应商应失去主标记"
    assert main.enabled is False
    assert backup.is_primary is True, "主标记应让给启用中的备供应商"
    llm_client._db_disabled_providers.pop("测试主供应商Z", None)


# ============================================================================
# 组 3：等待重试
# ============================================================================

@pytest.mark.asyncio
async def test_wait_retry_exhausts_and_raises():
    """三家全败 → 等待重试 2 轮 → 抛错，且回调被触发 2 次（含倒计时片）。"""
    client = LLMClient()
    calls = {"cb": 0, "retry_attempts": [], "countdown": 0}

    async def _fake_cb(message, level, attempt, total, extra=None):
        if level == "countdown":
            calls["countdown"] += 1
        else:
            calls["cb"] += 1
            calls["retry_attempts"].append(attempt)

    client._wait_retry_cb.set(_fake_cb)
    # 造 3 家可用候选，单家调用一律抛可重试错误
    client._candidate_providers = lambda *a, **k: [
        {"key": f"p{i}:m", "name": f"p{i}", "api_key": "k", "base_url": "u", "model": "m"}
        for i in range(3)
    ]

    async def _always_fail(*a, **k):
        from app.core.llm_client import _RetryableError
        raise _RetryableError("模拟失败")

    client._call_with_retry = _always_fail
    with patch("app.core.llm_client.asyncio.sleep", AsyncMock()) as mock_sleep:
        with pytest.raises(Exception) as ei:
            await client.call("hello", _skip_log=True)
    assert calls["cb"] == 2, f"应触发 2 次等待重试回调，实测 {calls['cb']}"
    assert calls["retry_attempts"] == [1, 2]
    assert calls["countdown"] > 0, "应推送倒计时片"
    # 每轮等待被切成 COUNTDOWN_TICK 片：2 轮 × (60/5) = 24 次 sleep
    assert mock_sleep.await_count == 2 * (client.WAIT_RETRY_INTERVAL // client.COUNTDOWN_TICK)
    assert "已重试 3 轮" in str(ei.value), f"错误信息应体现重试轮次: {ei.value}"


@pytest.mark.asyncio
async def test_wait_retry_recovers_on_second_round():
    """第 1 轮全败、第 2 轮成功 → 正常返回，不抛错。"""
    client = LLMClient()
    round_state = {"n": 0}

    def _cands(*a, **k):
        return [{"key": "p0:m", "name": "p0", "api_key": "k", "base_url": "u", "model": "m"}]

    client._candidate_providers = _cands

    async def _fail_then_ok(*a, **k):
        from app.core.llm_client import _RetryableError
        round_state["n"] += 1
        if round_state["n"] == 1:
            raise _RetryableError("第1轮失败")
        return ("OK结果", {}, "m")

    client._call_with_retry = _fail_then_ok
    with patch("app.core.llm_client.asyncio.sleep", AsyncMock()):
        content = await client.call("hello", _skip_log=True)
    assert content == "OK结果", "第 2 轮成功应返回结果"


@pytest.mark.asyncio
async def test_no_wait_when_all_disabled():
    """全部候选被持久化禁用（无候选）→ 不等待，直接失败。"""
    client = LLMClient()
    client._candidate_providers = lambda *a, **k: []
    from app.core.llm_client import LLMNoProviderAvailable
    with patch("app.core.llm_client.asyncio.sleep", AsyncMock()) as mock_sleep:
        with pytest.raises(LLMNoProviderAvailable):
            await client.call("hello", _skip_log=True)
    assert mock_sleep.await_count == 0, "无候选时不应等待重试"
