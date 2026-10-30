"""删除路径必须走出箱（v4.1.9）。

背景
----
单条删除原实现是「PG commit 后 to_thread 直删 Milvus，失败仅 warning」：
两系统非原子，Milvus 抖动即产生永久孤儿向量（只能人工跑
scripts/cleanup_orphan_vectors.py）。改为「业务行删除 + 出箱事件同事务提交」，
由 relay 幂等投递。

本文件锁死两条：
1. 删除用例时确实往同一个 session 里 add 了 action="delete" 的出箱事件；
2. 出箱事件与业务删除共用**一次** commit —— 若将来有人拆成两次 commit，
   原子性就没了，这条会失败。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.auth import CurrentUser
from app.api.testcases import delete_testcase


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, record):
        self.record = record
        self.added: list = []
        self.deleted: list = []
        self.commit_count = 0

    async def execute(self, stmt, *args, **kwargs):
        return _Result(self.record)

    async def get(self, model, pk):
        # 产品查询返回 None：可见性层放行，交给业务层（与真实语义一致）
        return None

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1


async def test_delete_testcase_enqueues_delete_event_in_same_commit():
    record = SimpleNamespace(id="TC-1", product_id="PROD-1", req_id="REQ-1")
    session = _FakeSession(record)

    await delete_testcase("TC-1", CurrentUser("alice", "user", 2), session)

    assert session.deleted == [record], "业务行必须先删"
    assert len(session.added) == 1, "必须恰好产生一条出箱事件"

    event = session.added[0]
    assert event.aggregate_type == "test_case"
    assert event.aggregate_id == "TC-1"
    assert event.action == "delete"
    assert session.commit_count == 1, "业务删除与出箱事件必须同一次 commit"


# ---------------------------------------------------------------------------
# 防回退哨兵：这两处曾长期是「commit 后直删 + 失败仅 warning」，
# 函数体太大、fake 成本高，用源码级断言锁住关键形态。
# ---------------------------------------------------------------------------

def test_fp_delete_enqueues_instead_of_direct_delete():
    """删 FP 端点是 require_write（普通用户可触发），必须走出箱。"""
    import inspect
    from app.api import requirements as req_api

    src = inspect.getsource(req_api.fp_delete)
    assert 'aggregate_type="fp_crsr"' in src, "删 FP 必须入箱 fp_crsr delete"
    assert "delete_requirement_from_kb" not in src, (
        "不得退回 commit 后直删 Milvus——失败仅 warning 会留下孤儿向量"
    )


def test_regenerate_fp_refresh_enqueues_instead_of_direct_delete():
    """regenerate 重新提取 FP 时清旧向量，同样必须与 FP 行删除同事务入箱。"""
    import inspect
    from app.core import requirement_tasks as rt

    # 该分支所在的函数体很大，取整个模块源码做粗粒度断言即可
    src = inspect.getsource(rt)
    assert 'created_by="regenerate_fp_refresh"' in src
    assert 'vector_store.delete_requirement_from_kb, f"FP-{old_fp_id}"' not in src
