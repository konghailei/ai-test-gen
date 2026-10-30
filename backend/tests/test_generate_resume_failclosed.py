"""续跑失败必须可见（v4.1.9）。

背景
----
`_find_resume_checkpoint` 原实现在查询异常时 `logger.warning` + `return None`。
调用方（generate 入口）把 None 理解为「没有可用 checkpoint」→ 直接从头生成，
把用户已跑了几小时的进度静默覆盖——违背「错误可见」，而且用户无从察觉。

本文件锁死两条边界：
1. 查询异常 → HTTPException(500)，绝不再静默返回 None；
2. 「查到但没有可续任务 / checkpoint 不完整」→ 仍返回 None（正常语义，
   调用方走全新生成），不能因为加了 fail-closed 就把正常空结果也变成 500。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import generate as gen_mod


class _FailingSession:
    """进入上下文后所有查询都炸——模拟 DB/连接池不可用。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, *args, **kwargs):
        raise RuntimeError("db is down")


class _EmptySession:
    """查询正常但没有命中任何可续任务。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, *args, **kwargs):
        class _Result:
            def scalars(self):
                return self

            def first(self):
                return None

        return _Result()


async def test_lookup_failure_raises_500_instead_of_none(monkeypatch):
    monkeypatch.setattr(gen_mod, "async_session", lambda: _FailingSession())

    with pytest.raises(HTTPException) as ei:
        await gen_mod._find_resume_checkpoint("REQ-1")

    assert ei.value.status_code == 500
    # 提示语必须说明后果，否则用户只会看到「服务器错误」然后重试覆盖
    assert "覆盖" in ei.value.detail


async def test_no_candidate_still_returns_none(monkeypatch):
    """正常空结果不能被 fail-closed 误伤。"""
    monkeypatch.setattr(gen_mod, "async_session", lambda: _EmptySession())

    assert await gen_mod._find_resume_checkpoint("REQ-1") is None
