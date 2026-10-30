"""删除路径出箱的真实落库验证（v4.1.9）。

fake session 只能证明「调用了 enqueue」，不能证明「真的写进了 vector_outbox 表」，
也不能证明它与业务删除在同一次事务里生效。本脚本用真实 PG + 完整 HTTP 栈跑一遍：

    1. 插一条测试用例（挂在现成需求下）；
    2. 调 DELETE /api/testcases/{id}（admin token）；
    3. 断言 vector_outbox 出现 action=delete、aggregate_type=test_case 的事件；
    4. 断言业务行已从 test_cases 消失；
    5. 清理 outbox 行。

用法（容器内，单进程一次 asyncio.run）：
    docker exec -w /app ai-test-gen-api python tests/e2e_vector_delete_outbox.py
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
from app.db.models import TestCase, VectorOutbox
from app.main import app


ADMIN_USER_ID = 1
REQ_ID = "REQ-1790234502710-3778"
PRODUCT_ID = "PROD-PROTO-TEST"


async def main() -> int:
    case_id = f"TC-E2EOUT-{uuid.uuid4().hex[:8].upper()}"
    print(f"[setup] case_id={case_id} req={REQ_ID}")

    # 1) 插用例（需求不存在会直接 FK 报错，比静默跳过更可靠）
    async with async_session() as s:
        s.add(TestCase(
            id=case_id,
            req_id=REQ_ID,
            product_id=PRODUCT_ID,
            title="E2E 出箱验证用例",
            type="functional",
            priority="P3",
            status="draft",
            case_json={"title": "E2E 出箱验证用例"},
        ))
        await s.commit()
    print("[setup] 用例已插入")

    ok = True
    try:
        token = AuthToken.create(username="e2e-admin", role="admin", user_id=ADMIN_USER_ID)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://e2e") as c:
            r = await c.delete(
                f"/api/testcases/{case_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        print(f"[http] DELETE /api/testcases/{case_id} → {r.status_code}")
        ok &= r.status_code == 200

        async with async_session() as s:
            events = (
                await s.execute(
                    select(VectorOutbox).where(VectorOutbox.aggregate_id == case_id)
                )
            ).scalars().all()
            still_there = (
                await s.execute(select(TestCase.id).where(TestCase.id == case_id))
            ).first()

        checks = [
            ("接口返回 200", r.status_code == 200),
            ("业务行已从 test_cases 删除", still_there is None),
            ("vector_outbox 恰好一条事件", len(events) == 1),
        ]
        if events:
            ev = events[0]
            checks += [
                ("action=delete", ev.action == "delete"),
                ("aggregate_type=test_case", ev.aggregate_type == "test_case"),
                ("aggregate_id 指向该用例", ev.aggregate_id == case_id),
            ]

        # 出箱链路验证：relay 跑在**真实 uvicorn 进程**里（interval=5s），
        # 只有它成功调用 Milvus 后状态才会变 done。若 relay 没启动、
        # 或 Milvus 删除抛错，这里会停在 pending/delivering/failed。
        # 状态枚举（models.VectorOutbox）：pending / delivering / done / failed
        print("[wait] 等待 relay 投递（最长 24s）...")
        final_status = None
        for _ in range(12):
            await asyncio.sleep(2)
            async with async_session() as s:
                final_status = (
                    await s.execute(
                        select(VectorOutbox.status).where(
                            VectorOutbox.aggregate_id == case_id
                        )
                    )
                ).scalar_one_or_none()
            if final_status == "done":
                break
        print(f"[wait] outbox 最终状态: {final_status}")
        checks.append(("relay 已投递到 Milvus（status=done）", final_status == "done"))

        for name, cond in checks:
            ok &= bool(cond)
            print(("  PASS  " if cond else "  FAIL  ") + name)
    finally:
        async with async_session() as s:
            await s.execute(sa_delete(VectorOutbox).where(VectorOutbox.aggregate_id == case_id))
            await s.execute(sa_delete(TestCase).where(TestCase.id == case_id))
            await s.commit()
        print("[teardown] 测试数据已清理")

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
