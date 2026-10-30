"""产品隔离 HTTP 层 e2e 验证（v4.1.9）。

不是 pytest 用例（无 test_ 前缀，不会被收集）——它需要真实 PG + 完整
HTTP 栈（middleware → router 守卫 → 端点），用于验证单测覆盖不到的部分：
守卫真的在请求链上生效，而不是只挂了个函数。

用法（容器内，单进程只跑一次）：
    docker exec -w /app ai-test-gen-api python tests/e2e_product_isolation.py

覆盖：
    1. 普通用户读不可见产品模块        → 403（products 路由守卫 + pid）
    2. 普通用户改不可见产品项目        → 403（守卫解析 proj_id）
    3. 普通用户查不可见产品统计        → 403（knowledge query 参数显式校验）
    4. 依赖图谱不返回不可见产品模块    → 200 且不含目标模块（query 过滤）
    5. 普通用户读可见产品              → 200（守卫不能误伤）
    6. admin 读/写不可见产品           → 200（守卫对 admin 放行）
    7. 评审报告列表不泄漏不可见产品    → 200 且 items 无该产品

数据自清理：产品/项目/模块按依赖顺序删除。
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, "/app")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.auth import AuthToken
from app.db.database import async_session
from app.db.models import Module, Project, Product
from app.main import app


NORMAL_USER_ID = 99999
ADMIN_USER_ID = 1

_results: list[tuple[bool, str, str]] = []


def _check(name: str, cond: bool, extra: str = "") -> None:
    _results.append((bool(cond), name, extra))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))


async def _probe_products(pid_a: str, pid_b: str, proj_b: str, mod_b: str) -> None:
    user_token = AuthToken.create(username="e2e-user", role="user", user_id=NORMAL_USER_ID)
    admin_token = AuthToken.create(username="e2e-admin", role="admin", user_id=ADMIN_USER_ID)
    hu = {"Authorization": f"Bearer {user_token}"}
    ha = {"Authorization": f"Bearer {admin_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://e2e") as c:
        r = await c.get(f"/api/products/{pid_b}/modules", headers=hu)
        _check("普通用户读不可见产品模块 → 403", r.status_code == 403, f"got {r.status_code}")

        r = await c.put(f"/api/products/projects/{proj_b}", headers=hu, json={"name": "hijacked"})
        _check("普通用户改不可见产品项目 → 403", r.status_code == 403, f"got {r.status_code}")

        r = await c.get("/api/knowledge/stats", params={"product_id": pid_b}, headers=hu)
        _check("普通用户查不可见产品统计 → 403", r.status_code == 403, f"got {r.status_code}")

        r = await c.get("/api/review-reports", params={"product_id": pid_b}, headers=hu)
        _check("普通用户查不可见产品评审报告 → 403", r.status_code == 403, f"got {r.status_code}")

        r = await c.get("/api/products/dependency-graph", headers=hu)
        body = r.json().get("data", {}) if r.status_code == 200 else {}
        node_ids = {n.get("id") for n in body.get("nodes", [])}
        _check(
            "依赖图谱不返回不可见产品模块",
            r.status_code == 200 and mod_b not in node_ids,
            f"got {r.status_code}, nodes={len(node_ids)}",
        )

        r = await c.get(f"/api/products/{pid_a}/projects", headers=hu)
        _check("普通用户读可见产品 → 200", r.status_code == 200, f"got {r.status_code}")

        r = await c.get(f"/api/products/{pid_b}/modules", headers=ha)
        _check("admin 读不可见产品 → 200", r.status_code == 200, f"got {r.status_code}")

        r = await c.put(
            f"/api/products/projects/{proj_b}", headers=ha, json={"description": "admin-ok"}
        )
        _check("admin 改不可见产品项目 → 200", r.status_code == 200, f"got {r.status_code}")

        r = await c.get("/api/review-reports", headers=hu)
        items = r.json().get("data", {}).get("items", []) if r.status_code == 200 else []
        leaked = [i for i in items if i.get("product_id") == pid_b]
        _check(
            "评审报告列表不泄漏不可见产品",
            r.status_code == 200 and not leaked,
            f"got {r.status_code}, leaked={len(leaked)}",
        )


async def main() -> int:
    suffix = uuid.uuid4().hex[:8].upper()
    pid_a = f"PROD-E2EA-{suffix}"
    pid_b = f"PROD-E2EB-{suffix}"
    proj_b = f"PROJ-E2E-{suffix}"
    mod_b = f"MOD-E2E-{suffix}"

    print(f"[setup] A={pid_a} B={pid_b}（B 仅 admin 可见）")
    # 模型之间没有声明 relationship，同一次 commit 内无法保证 products 先于
    # projects/modules 插入（FK 会失败）。分两步提交。
    async with async_session() as s:
        s.add(Product(
            id=pid_a, name=f"E2E-A-{suffix}", description="e2e isolation",
            visible_to_user_ids=[],
        ))
        s.add(Product(
            id=pid_b, name=f"E2E-B-{suffix}", description="e2e isolation",
            visible_to_user_ids=[ADMIN_USER_ID],
        ))
        await s.commit()

    async with async_session() as s:
        s.add(Project(
            id=proj_b, product_id=pid_b, name=f"E2E项目-{suffix}",
            description="", version="9.9",
        ))
        s.add(Module(
            id=mod_b, product_id=pid_b, name=f"E2E模块-{suffix}", description="",
        ))
        await s.commit()

    try:
        await _probe_products(pid_a, pid_b, proj_b, mod_b)
    finally:
        async with async_session() as s:
            await s.execute(delete(Module).where(Module.id == mod_b))
            await s.execute(delete(Project).where(Project.id == proj_b))
            await s.execute(delete(Product).where(Product.id.in_([pid_a, pid_b])))
            await s.commit()
        print("[teardown] 测试数据已清理")

    failed = [r for r in _results if not r[0]]
    print(f"\nRESULT: {len(_results) - len(failed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
