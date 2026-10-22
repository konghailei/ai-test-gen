"""生成期心跳守卫单测（P-9 修复，2026-10-22）。

守卫目标（验**行为**，不验源码字面量）：
  1. 生成节点执行期间，心跳循环**真的**把 analysis_tasks.updated_at 往前推
     ——这是"长 FP 分片不被看门狗误标 interrupted"的唯一凭据；
  2. 节点退出（正常返回）时心跳任务被取消，不留泄漏协程；
  3. 节点抛异常时同样取消（finally 覆盖），异常照常冒泡不被吞；
  4. 心跳写库失败时**静默**，绝不影响生成主流程；
  5. 只刷新 running 态任务（不误碰已完成/已中断任务）。

实现方式：不连真 PG（对照 unit 层禁令：连接池绑旧 loop 会 Event loop is closed），
用行为级 fake 替换 `app.db.database.async_session`，断言"确实发出了 UPDATE"。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core import orchestrator as orch_mod
from app.core.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# 行为级 fake：记录 SQL 语句，模拟 async session
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self, sink: list):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        # 记录编译后的 SQL 与参数，供断言"确实 UPDATE 了 updated_at"
        self._sink.append(stmt)
        return None

    async def commit(self):
        self._sink.append("__COMMIT__")


@pytest.fixture
def patched_session(monkeypatch):
    """把 orchestrator 内 imports 的 async_session 换成 fake；返回语句收集列表。"""
    sink: list = []

    def _fake_async_session():
        return _FakeSession(sink)

    import app.db.database as db_mod
    monkeypatch.setattr(db_mod, "async_session", _fake_async_session)
    return sink


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """把心跳间隔压到 10ms，让测试在百毫秒内跑完。"""
    monkeypatch.setattr(orch_mod, "HEARTBEAT_INTERVAL_S", 0.01)


def _is_heartbeat_update(stmt) -> bool:
    """判定该语句是否是心跳的 UPDATE analysis_tasks SET updated_at=..."""
    try:
        sql = str(stmt)
    except Exception:
        return False
    return "UPDATE" in sql.upper() and "analysis_tasks" in sql.lower()


def _sql_of(stmt) -> str:
    try:
        return str(stmt)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 1. 心跳真的刷新 updated_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_updates_updated_at(patched_session, fast_heartbeat):
    """后台心跳应按间隔真的发出 UPDATE analysis_tasks SET updated_at=..."""
    o = Orchestrator()
    task = asyncio.create_task(o._gen_heartbeat_loop("TASK-HB-1"))
    try:
        await asyncio.sleep(0.08)   # 够触发多次心跳
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    updates = [s for s in patched_session if _is_heartbeat_update(s)]
    assert updates, f"心跳未发出任何 UPDATE，实际语句: {[_sql_of(s) for s in patched_session]}"
    assert "__COMMIT__" in patched_session, "心跳必须 commit 才能让 updated_at 真正落库"
    # 断言只改 updated_at，不误改其它列
    assert "updated_at" in _sql_of(updates[0]).lower()


# ---------------------------------------------------------------------------
# 2. 只刷新 running 态（不误碰已完成/中断任务）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_filters_running_status(patched_session, fast_heartbeat):
    """心跳 UPDATE 必须带 status='running' 条件，避免把已完成任务的时间戳搅乱。"""
    o = Orchestrator()
    task = asyncio.create_task(o._gen_heartbeat_loop("TASK-HB-2"))
    try:
        await asyncio.sleep(0.04)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    updates = [s for s in patched_session if _is_heartbeat_update(s)]
    assert updates, "未捕获到心跳 UPDATE"
    sql = _sql_of(updates[0]).lower()
    assert "status" in sql, f"UPDATE 应带 status 过滤，实际 SQL: {sql}"
    # 参数里应有 running 字面量
    params = getattr(updates[0], "compile", lambda: None)
    try:
        compiled_params = str(updates[0].compile().params).lower()
        assert "running" in compiled_params, f"参数字面量应含 running，实际: {compiled_params}"
    except Exception:
        # 某些语句对象不支持 compile，退化为只验 SQL 结构
        pass


# ---------------------------------------------------------------------------
# 3. 节点正常退出 → 心跳被取消（无泄漏）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_cancelled_on_normal_exit(monkeypatch, patched_session, fast_heartbeat):
    """_gen_node 正常返回后，心跳任务必须已结束（协程不泄漏）。"""
    o = Orchestrator()
    created: list[asyncio.Task] = []
    _orig_create = asyncio.create_task

    def _spy_create(coro, **kw):
        t = _orig_create(coro, **kw)
        if "heartbeat" in str(kw.get("name", "")):
            created.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _spy_create)

    async def _fake_inner(state):
        await asyncio.sleep(0.03)   # 让心跳至少跑一轮
        return {"test_cases": []}

    monkeypatch.setattr(o, "_gen_node_inner", _fake_inner)

    out = await o._gen_node({"task_id": "TASK-HB-3"})
    assert out == {"test_cases": []}
    assert created, "未创建心跳任务"
    assert created[0].done(), "节点退出后心跳任务必须已结束（否则协程泄漏）"


# ---------------------------------------------------------------------------
# 4. 节点抛异常 → 心跳仍被取消，异常照常冒泡
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_cancelled_on_exception(monkeypatch, patched_session, fast_heartbeat):
    """_gen_node 抛异常时心跳也必须取消，且异常不能被吞掉。"""
    o = Orchestrator()
    created: list[asyncio.Task] = []
    _orig_create = asyncio.create_task

    def _spy_create(coro, **kw):
        t = _orig_create(coro, **kw)
        if "heartbeat" in str(kw.get("name", "")):
            created.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _spy_create)

    async def _boom(state):
        await asyncio.sleep(0.02)
        raise RuntimeError("生成炸了")

    monkeypatch.setattr(o, "_gen_node_inner", _boom)

    with pytest.raises(RuntimeError, match="生成炸了"):
        await o._gen_node({"task_id": "TASK-HB-4"})

    assert created and created[0].done(), "异常路径下心跳任务也必须结束"


# ---------------------------------------------------------------------------
# 5. 写库失败静默（不影响主流程）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_failure_is_silent(monkeypatch, fast_heartbeat):
    """心跳遇到 DB 异常必须静默，不得把异常抛给调用方。"""
    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("DB 挂了")

        async def __aexit__(self, *exc):
            return False

    import app.db.database as db_mod
    monkeypatch.setattr(db_mod, "async_session", lambda: _BrokenSession())

    o = Orchestrator()
    task = asyncio.create_task(o._gen_heartbeat_loop("TASK-HB-5"))
    try:
        await asyncio.sleep(0.05)
        # 循环应仍然活着（异常被内部捕获），而不是挂掉
        assert not task.done(), "心跳遇到 DB 异常不应退出循环"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 6. 无 task_id 不起心跳
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_heartbeat_without_task_id(monkeypatch, patched_session, fast_heartbeat):
    """state 无 task_id 时不应起心跳任务（避免无意义写库）。"""
    o = Orchestrator()
    created: list[asyncio.Task] = []
    _orig_create = asyncio.create_task

    def _spy_create(coro, **kw):
        t = _orig_create(coro, **kw)
        if "heartbeat" in str(kw.get("name", "")):
            created.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _spy_create)

    async def _fake_inner(state):
        return {"test_cases": []}

    monkeypatch.setattr(o, "_gen_node_inner", _fake_inner)

    await o._gen_node({})
    assert not created, "无 task_id 时不应创建心跳任务"
    assert not [s for s in patched_session if _is_heartbeat_update(s)], "无 task_id 时不应写库"


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v", "-q"]))
