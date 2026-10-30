"""范围删除出箱的真实落库验证（v4.1.9）。

对应改动：删产品 / 删项目 / 按需求批量删用例，从「commit 后直删 Milvus」
改为「业务删除 + scope 事件同事务入箱」。scope 事件没有单 aggregate_id，
删什么维度由 payload.scope_type 决定，relay 调用对应批量方法。

本脚本验证 product 分支的完整链路（project / req 分支由单测覆盖）：
    1. 建一个空产品（挂 admin 可见）；
    2. 调 DELETE /api/products/{pid}?confirm_name=...；
    3. 断言 vector_outbox 出现 aggregate_type=scope、payload.scope_type=product 的事件；
    4. 等 relay 把它投递成 done（done = 批量删除调用没抛错）；
    5. 清理 outbox 行。

用法：docker exec -w /app ai-test-gen-api python tests/e2e_scope_delete_outbox.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, "/app")

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete, select

from app.core.auth import AuthToken
from app.db.database import async_session
from app.db.models import Product, VectorOutbox
from app.main import app


ADMIN_USER_ID = 1


async def main() -> int:
    suffix = uuid.uuid4().hex[:8].upper()
    pid = f"PROD-E2ESCOPE-{suffix}"
    pname = f"E2E-SCOPE-{suffix}"
    print(f"[setup] product={pid}")

    async with async_session() as s:
        s.add(Product(id=pid, name=pname, description="scope delete e2e", visible_to_user_ids=[]))
        await s.commit()

    ok = True
    try:
        token = AuthToken.create(username="e2e-admin", role="admin", user_id=ADMIN_USER_ID)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://e2e") as c:
            r = await c.delete(
                f"/api/products/{pid}",
                params={"confirm_name": pname},
                headers={"Authorization": f"Bearer {token}"},
            )
        print(f"[http] DELETE /api/products/{pid} → {r.status_code}")

        async with async_session() as s:
            events = (
                await s.execute(
                    select(VectorOutbox).where(VectorOutbox.aggregate_id == f"product:{pid}")
                )
            ).scalars().all()
            still_there = (
                await s.execute(select(Product.id).where(Product.id == pid))
            ).first()

        checks = [
            ("接口返回 200", r.status_code == 200),
            ("产品行已删除", still_there is None),
            ("恰好一条 scope 事件", len(events) == 1),
        ]
        if events:
            ev = events[0]
            checks += [
                ("aggregate_type=scope", ev.aggregate_type == "scope"),
                ("action=delete", ev.action == "delete"),
                ("payload.scope_type=product", (ev.payload or {}).get("scope_type") == "product"),
                ("payload.scope_id 指向该产品", (ev.payload or {}).get("scope_id") == pid),
            ]

        # relay 跑在真实 uvicorn 进程里（interval=5s）。状态枚举：
        # pending / delivering / done / failed —— done 才代表批量删除调用成功。
        print("[wait] 等待 relay 投递（最长 24s）...")
        final_status = None
        for _ in range(12):
            await asyncio.sleep(2)
            async with async_session() as s:
                final_status = (
                    await s.execute(
                        select(VectorOutbox.status).where(
                            VectorOutbox.aggregate_id == f"product:{pid}"
                        )
                    )
                ).scalar_one_or_none()
            if final_status == "done":
                break
        print(f"[wait] outbox 最终状态: {final_status}")
        checks.append(("relay 投递完成（status=done）", final_status == "done"))

        for name, cond in checks:
            ok &= bool(cond)
            print(("  PASS  " if cond else "  FAIL  ") + name)
    finally:
        async with async_session() as s:
            await s.execute(sa_delete(VectorOutbox).where(VectorOutbox.aggregate_id == f"product:{pid}"))
            await s.execute(sa_delete(Product).where(Product.id == pid))
            await s.commit()
        print("[teardown] 测试数据已清理")

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
