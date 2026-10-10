"""DEF-2 FP 删除接口回归（v3.5.7 修复验证）。

复现路径（修复前必 500）：
  DELETE /api/requirements/{req_id}/fp/{fp_id}
    → 级联清理 SQL `:fp_id::text` 触发 asyncpg 语法错误
    → 事务中毒 → 后续 SELECT 全部 InFailedSQLTransactionError → 500

本脚本覆盖两件事：
  ① 双需求夹具的正向路径：需求 A 的 FP X 被需求 B 的 srr_json.impact_analysis 引用，
     删除 X 应 200，且 B 的引用被级联清除（跨需求清理真正生效）。
  ② 失败路径注入：mock 清理语句抛错 → 接口仍应完成 FP 删除（rollback 生效，不 500）。

运行方式（容器内）：
  docker exec -i ai-test-gen-api python - < backend/tests/test_phase7_fp_delete.py
或直接拷入容器 /tmp 运行（勿放 /app，避免触发 reload 杀后台任务）。
"""

from __future__ import annotations

import asyncio
import json
import sys

from httpx import AsyncClient, ASGITransport

# 夹具 ID（测试专用前缀，避免与真实数据冲突）
PROD = "PROD-DEF2-TEST"
REQ_A = "REQ-DEF2-A"
REQ_B = "REQ-DEF2-B"
FP_X = "REQ-DEF2-A-FP1"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail and not ok else ""))


async def _setup(session):
    """建立夹具：产品 + 需求 A + FP X + 需求 B（impact_analysis 引用 X）。"""
    from sqlalchemy import delete
    from app.db.models import Requirement, FunctionalPoint, Module, Product

    # 清理旧夹具（子表先删）
    for rid in (REQ_A, REQ_B):
        await session.execute(delete(FunctionalPoint).where(FunctionalPoint.requirement_id == rid))
        await session.execute(delete(Requirement).where(Requirement.id == rid))
    await session.execute(delete(Module).where(Module.product_id == PROD))
    await session.execute(delete(Product).where(Product.id == PROD))
    await session.commit()

    session.add(Product(id=PROD, name="DEF2 测试产品"))
    await session.flush()

    mod = Module(id="MOD-DEF2-1", product_id=PROD, name="删除测试模块")
    session.add(mod)
    await session.flush()

    # 先建需求（FP 有 requirement_id 外键，须后插）
    req_a = Requirement(
        id=REQ_A, product_id=PROD, title="DEF2-A", priority="P2",
        module="删除测试模块", modules=[], version=1, status="pending_review",
        srr_json={"requirement": {"id": REQ_A, "title": "DEF2-A"}, "impact_analysis": []},
        raw_text="", raw_images=[], source_type="multi", created_by="test",
    )
    req_b = Requirement(
        id=REQ_B, product_id=PROD, title="DEF2-B", priority="P2",
        module="", modules=[], version=1, status="pending_review",
        srr_json={
            "requirement": {"id": REQ_B, "title": "DEF2-B"},
            "impact_analysis": [
                {"fp_id": FP_X, "fp_name": "被删除功能点", "is_affected": True, "confirmed_at": "2026-10-10T00:00:00Z"},
                {"fp_id": "OTHER-KEEP", "fp_name": "保留项", "is_affected": True, "confirmed_at": "2026-10-10T00:00:00Z"},
            ],
        },
        raw_text="", raw_images=[], source_type="multi", created_by="test",
    )
    session.add(req_a)
    session.add(req_b)
    await session.flush()

    fp_a = FunctionalPoint(
        id=FP_X, requirement_id=REQ_A, module_id="MOD-DEF2-1", modules=["MOD-DEF2-1"],
        name="被删除功能点", fp_type="workflow", priority="P2",
        srr_json={"summary": "test"}, atomicity_check={}, test_hints=[],
        status="pending_review",
    )
    session.add(fp_a)
    await session.commit()


async def _cleanup(session):
    from sqlalchemy import delete
    from app.db.models import Requirement, FunctionalPoint, Module, Product
    for rid in (REQ_A, REQ_B):
        await session.execute(delete(FunctionalPoint).where(FunctionalPoint.requirement_id == rid))
        await session.execute(delete(Requirement).where(Requirement.id == rid))
    await session.execute(delete(Module).where(Module.product_id == PROD))
    await session.execute(delete(Product).where(Product.id == PROD))
    await session.commit()


async def main() -> int:
    from app.db.database import async_session
    from app.core.auth import get_default_token
    from app.main import app

    token = get_default_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with async_session() as session:
        await _setup(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        # ---- 用例 1：正向路径（修复前 500）----
        r = await c.delete(f"/api/requirements/{REQ_A}/fp/{FP_X}")
        check("1. 删除被跨需求引用的 FP 返回 200", r.status_code == 200,
              f"status={r.status_code} body={r.text[:200]}")

        # ---- 用例 2：跨需求级联清理生效 ----
        async with async_session() as session:
            from sqlalchemy import select
            from app.db.models import Requirement
            rb = (await session.execute(
                select(Requirement.srr_json).where(Requirement.id == REQ_B)
            )).scalar_one_or_none()
        imp = (rb or {}).get("impact_analysis", []) if isinstance(rb, dict) else []
        ids = [e.get("fp_id") for e in imp]
        check("2. 需求 B 中 FP_X 引用被级联清除", FP_X not in ids, f"remaining={ids}")
        check("3. 需求 B 中无关条目保留", "OTHER-KEEP" in ids, f"remaining={ids}")

        # ---- 用例 4：FP 行已删 ----
        async with async_session() as session:
            from app.db.models import FunctionalPoint
            gone = await session.get(FunctionalPoint, FP_X)
        check("4. functional_points 中 FP 行已删除", gone is None)

        # ---- 用例 5：重复删除返回 404 ----
        r2 = await c.delete(f"/api/requirements/{REQ_A}/fp/{FP_X}")
        check("5. 重复删除返回 404", r2.status_code == 404, f"status={r2.status_code}")

        # ---- 用例 6：失败路径注入（mock 清理语句抛错 → 仍应完成删除）----
        async with async_session() as session:
            await _setup(session)
        from unittest.mock import patch
        import sqlalchemy
        _orig_execute = sqlalchemy.ext.asyncio.AsyncSession.execute

        async def _boom(self, stmt, *a, **kw):
            s = str(stmt)
            if "jsonb_set" in s and "impact_analysis" in s:
                raise RuntimeError("injected cleanup failure (DEF-2 失败路径)")
            return await _orig_execute(self, stmt, *a, **kw)

        with patch.object(sqlalchemy.ext.asyncio.AsyncSession, "execute", _boom):
            r3 = await c.delete(f"/api/requirements/{REQ_A}/fp/{FP_X}")
        check("6. 清理语句注入异常后仍返回 200（rollback 生效）", r3.status_code == 200,
              f"status={r3.status_code} body={r3.text[:200]}")
        async with async_session() as session:
            from app.db.models import FunctionalPoint
            gone2 = await session.get(FunctionalPoint, FP_X)
        check("7. 注入异常后 FP 仍被删除", gone2 is None)

    async with async_session() as session:
        await _cleanup(session)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nDEF-2 回归: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
