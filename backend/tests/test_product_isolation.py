"""产品可见性隔离回归（v4.1.9）。

背景
----
v3.1.3 P1-1 只做了「查询侧过滤」；requirements.py 靠路由级守卫覆盖了全部
``/{req_id}/...`` 端点，但 products / review_reports / knowledge / dedup_removals
的写端点与列表端点仍以「有写权限即可」放行——ID 形如 ``PROJ-时间戳-序号``，
可枚举，任何 write 用户都能跨产品操作。

本文件锁死四件事：

1. ``assert_product_visible`` 的五条语义（admin 放行 / 空产品放行 / 名单为空
   放行 / 名单命中放行 / 名单未命中 403 / 产品不存在放行交给 404）；
2. ``product_guard`` 能把 proj_id / mod_id / fp_id / version_pk 解析到正确产品，
   且资源不存在时返回 None（不把 404 变成 403）；
3. 两个路由级守卫确实挂在 router 上——新增端点自动继承，不依赖逐个补；
4. products 路由用到的每个路径参数名都在守卫的解析范围内（防止未来新增
   ``{project_id}`` 这类同义不同名的参数漏解析）。

行为级 fake session，不连真 PG（与 test_kqa_approve_idempotent.py 同风格）。
真实落库与 HTTP 403 由容器内 e2e 另验。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import product_guard
from app.core.auth import CurrentUser
from app.core.product_access import assert_product_visible, get_visible_product_ids
from app.db.models import (
    Module,
    Product,
    Project,
    Requirement,
    ReviewReportRecord,
    TestCase as _TestCaseModel,  # 别名：避免 pytest 把 ORM 类当成测试类收集
)


ADMIN = CurrentUser("admin", "admin", 1)
USER_A = CurrentUser("alice", "user", 2)
USER_B = CurrentUser("bob", "user", 3)


class _Result:
    """够用的 Result 替身：覆盖 scalar_one_or_none / scalars / all。"""

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value or []


class _FakeSession:
    """记录调用次数，按预置值返回——不解析 SQL。"""

    def __init__(self, *, products: dict | None = None, scalar_values: list | None = None):
        self.products = products or {}
        self._values = list(scalar_values or [])
        self.get_calls = 0
        self.execute_calls = 0

    async def get(self, model, pk):
        self.get_calls += 1
        return self.products.get(pk)

    async def execute(self, stmt, *args, **kwargs):
        self.execute_calls += 1
        return _Result(self._values.pop(0) if self._values else None)


def _product(visible_to: list[int] | None):
    """Product 的轻量替身：只需要 visible_to_user_ids。"""
    return SimpleNamespace(visible_to_user_ids=visible_to)


# ---------------------------------------------------------------------------
# 1. assert_product_visible 语义
# ---------------------------------------------------------------------------

async def test_admin_bypasses_without_querying():
    session = _FakeSession()
    await assert_product_visible(session, ADMIN, "PROD-X")
    assert session.get_calls == 0, "admin 不应产生可见性查询"


async def test_empty_product_id_passes_to_business_layer():
    session = _FakeSession()
    await assert_product_visible(session, USER_A, None)
    assert session.get_calls == 0


async def test_legacy_empty_allowlist_is_open():
    """存量产品（名单为空）必须保持全员可见，否则升级即全站报错。"""
    session = _FakeSession(products={"PROD-1": _product([])})
    await assert_product_visible(session, USER_B, "PROD-1")


async def test_listed_user_passes():
    session = _FakeSession(products={"PROD-1": _product([2, 9])})
    await assert_product_visible(session, USER_A, "PROD-1")


async def test_outsider_gets_403():
    session = _FakeSession(products={"PROD-1": _product([2])})
    with pytest.raises(HTTPException) as ei:
        await assert_product_visible(session, USER_B, "PROD-1")
    assert ei.value.status_code == 403


async def test_missing_product_passes_so_endpoint_returns_404():
    """产品不存在时可见性层不报错——否则会把 404 语义变成 403。"""
    session = _FakeSession()
    await assert_product_visible(session, USER_B, "PROD-GONE")


# ---------------------------------------------------------------------------
# 2. get_visible_product_ids：None 代表不过滤，空列表代表全不可见
# ---------------------------------------------------------------------------

async def test_visible_ids_admin_is_unrestricted():
    session = _FakeSession()
    assert await get_visible_product_ids(session, ADMIN) is None
    assert session.execute_calls == 0


async def test_visible_ids_filters_by_membership():
    session = _FakeSession(
        scalar_values=[[("PROD-1", []), ("PROD-2", [2]), ("PROD-3", [3])]]
    )
    assert await get_visible_product_ids(session, USER_A) == ["PROD-1", "PROD-2"]


async def test_visible_ids_can_be_empty():
    """一个产品都不可见时必须返回空列表（不能返回 None，否则等于全量放行）。"""
    session = _FakeSession(scalar_values=[[("PROD-1", [3])]])
    assert await get_visible_product_ids(session, USER_A) == []


# ---------------------------------------------------------------------------
# 3. product_guard 解析链路
# ---------------------------------------------------------------------------

async def test_project_resolves_to_product():
    session = _FakeSession(scalar_values=["PROD-9"])
    assert await product_guard.product_id_of_project(session, "PROJ-1") == "PROD-9"


async def test_project_missing_returns_none():
    session = _FakeSession(scalar_values=[None])
    assert await product_guard.product_id_of_project(session, "PROJ-X") is None


async def test_module_resolves_to_product():
    session = _FakeSession(scalar_values=["PROD-4"])
    assert await product_guard.product_id_of_module(session, "MOD-1") == "PROD-4"


async def test_fp_chains_through_requirement():
    """功能点表没有 product_id，必须经 requirement_id 回溯。"""
    session = _FakeSession(scalar_values=["REQ-1", "PROD-7"])
    assert await product_guard.product_id_of_fp(session, "FP-1") == "PROD-7"


async def test_dependency_version_chains_through_project():
    """版本快照表只有 project_id，没有冗余 product_id。"""
    session = _FakeSession(scalar_values=["PROJ-1", "PROD-5"])
    assert await product_guard.product_id_of_dependency_version(session, 12) == "PROD-5"


async def test_version_pk_none_short_circuits():
    session = _FakeSession()
    assert await product_guard.product_id_of_dependency_version(session, None) is None
    assert session.execute_calls == 0


async def test_report_uses_own_product_column():
    session = _FakeSession(scalar_values=["PROD-2"])
    assert await product_guard.product_id_of_report(session, "RR-1") == "PROD-2"


async def test_dedup_removal_chains_through_requirement():
    session = _FakeSession(scalar_values=["REQ-3", "PROD-6"])
    assert await product_guard.product_id_of_dedup_removal(session, "DR-1") == "PROD-6"


# ---------------------------------------------------------------------------
# 4. 守卫挂载与路径参数覆盖（防未来新增端点漏挂）
# ---------------------------------------------------------------------------

def _dependency_names(router) -> set[str]:
    return {
        getattr(d.dependency, "__name__", "")
        for d in router.dependencies
    }


def test_products_router_carries_scope_guard():
    from app.api import products

    assert "_guard_product_scope" in _dependency_names(products.router), (
        "products.router 必须挂 _guard_product_scope——"
        "否则 9 个 require_write 写端点（项目归档/重激活/go-live-date/模块/依赖边）"
        "会重新暴露为跨产品可操作"
    )


def test_requirements_router_carries_visibility_guard():
    from app.api import requirements

    assert "_guard_product_visibility" in _dependency_names(requirements.router)


def test_products_path_params_are_all_resolvable():
    """守卫按参数名分派解析。新增端点若用了未登记的参数名，守卫会静默放行。"""
    from app.api import products

    # 与 _guard_product_scope 的分派分支一一对应
    handled = {"pid", "product_id", "proj_id", "mod_id", "fp_id", "version_pk", "v1"}
    # v2 只在 diff 端点出现，由端点内部显式做同产品校验
    handled_inline = {"v2"}

    used: set[str] = set()
    for route in products.router.routes:
        used.update(re.findall(r"\{(\w+)\}", getattr(route, "path", "")))

    unknown = used - handled - handled_inline
    assert not unknown, (
        f"products 路由出现守卫未解析的路径参数: {sorted(unknown)}。"
        "请在 _guard_product_scope 里补解析分支，或在端点内显式校验后加入 handled_inline。"
    )


def test_guard_reads_real_product_column():
    """字段名拼错会让守卫永远返回 None（静默放行）——这里锁死列名。"""
    # Product 自身是被校验对象，只有可见名单列
    assert "visible_to_user_ids" in Product.__table__.columns
    # 解析链路依赖的这些表必须真的有 product_id 列
    for model in (Project, Module, Requirement, _TestCaseModel, ReviewReportRecord):
        assert "product_id" in model.__table__.columns, (
            f"{model.__name__} 缺少 product_id 列——守卫解析会返回 None 并静默放行"
        )
