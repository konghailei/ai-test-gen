"""产品可见性矩阵缓存（v4.1.9）。

`get_visible_product_ids` 从「每次全表扫 Product」改为「5s 进程内缓存 + 写路径主动失效」。
本文件锁死四条语义：

1. TTL 内重复调用只查一次库；
2. 不同用户**共用同一份原始矩阵**（过滤在调用时做），不会按用户各扫一遍；
3. `invalidate_product_cache()` 立即生效，不等 TTL；
4. admin 走短路，永不查库。

第 5 条最关键：新建产品后若不主动失效，5s 内它「不在矩阵里」，
`get_visible_product_ids` 会把它漏掉 —— 表现为「刚建的产品自己看不到」。
"""

from __future__ import annotations

import pytest

from app.core.auth import CurrentUser
from app.core.product_access import get_visible_product_ids, invalidate_product_cache


ADMIN = CurrentUser("admin", "admin", 1)
USER_A = CurrentUser("alice", "user", 2)
USER_B = CurrentUser("bob", "user", 3)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.execute_calls = 0

    async def execute(self, stmt, *args, **kwargs):
        self.execute_calls += 1
        return _Result(self.rows)


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_product_cache()
    yield
    invalidate_product_cache()


async def test_matrix_cached_within_ttl():
    session = _FakeSession([("P1", []), ("P2", [2])])

    first = await get_visible_product_ids(session, USER_A)
    second = await get_visible_product_ids(session, USER_A)

    assert first == second == ["P1", "P2"]
    assert session.execute_calls == 1, "TTL 内第二次调用不应再查库"


async def test_different_users_share_one_matrix():
    """缓存的是原始矩阵，不是「按用户的可见列表」——两个用户只扫一次表。"""
    session = _FakeSession([("P1", []), ("P2", [2]), ("P3", [3])])

    a = await get_visible_product_ids(session, USER_A)
    b = await get_visible_product_ids(session, USER_B)

    assert a == ["P1", "P2"]
    assert b == ["P1", "P3"]
    assert session.execute_calls == 1


async def test_invalidate_forces_reload():
    session = _FakeSession([("P1", [])])
    await get_visible_product_ids(session, USER_A)

    invalidate_product_cache()
    await get_visible_product_ids(session, USER_A)

    assert session.execute_calls == 2


async def test_admin_never_queries():
    session = _FakeSession([])
    assert await get_visible_product_ids(session, ADMIN) is None
    assert session.execute_calls == 0


async def test_new_product_invisible_until_invalidated():
    """新建产品必须由写路径主动失效，否则 TTL 内连创建者自己都看不到。"""
    session = _FakeSession([("P1", [])])
    assert await get_visible_product_ids(session, USER_A) == ["P1"]

    # 产品 P2 刚建好，但缓存还没失效
    session.rows = [("P1", []), ("P2", [])]
    assert await get_visible_product_ids(session, USER_A) == ["P1"]

    # products.create_product 会调这个
    invalidate_product_cache()
    assert await get_visible_product_ids(session, USER_A) == ["P1", "P2"]
