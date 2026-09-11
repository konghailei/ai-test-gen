"""v3.2.13b 上线时间变更 → 功能点最新版用例集重整合 —— 严格的两项目翻转验证。

场景：同一产品下两个已归档项目 A/B 共享同一功能点名，各自有 approved 用例。
  - 先让 B 的上线时间较新 → 用例集应归属 B
  - 再把 A 的上线时间改到比 B 更新 → 用例集应翻转归属 A

这直接验证「调整上线时间会导致整合产物失效」这一风险点是否已被修复。

用法：docker exec ai-test-gen-api python /app/tests/_e2e_fpcase_regolive.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRODUCT = "PROD-53145-856"
PROJ_A = "PROJ-856-95906-794"   # 工单系统权限回收v2.1c (ver 2.1c)
PROJ_B = "PROJ-856-97479-144"   # 工单系统权限回收v2.1  (ver 2.1)
REQ_A = "REQ-1787730895940-3104"
REQ_B = "REQ-1787725997548-7538"
FP_NAME = "权限回收记录"
CASE_A = "TC-E2E-GOLIVE-A"
CASE_B = "TC-E2E-GOLIVE-B"

DATE_B = "2026-11-01T09:00"     # 先让 B 更新
DATE_A = "2026-12-01T09:00"     # 再让 A 反超


async def _fpc_row(session):
    from app.db.models import FunctionalPointCases
    from sqlalchemy import select
    return (await session.execute(
        select(FunctionalPointCases).where(
            FunctionalPointCases.product_id == PRODUCT,
            FunctionalPointCases.fp_name == FP_NAME,
        )
    )).scalar_one_or_none()


async def _ensure_case(session, case_id, req_id, title):
    """插入一条 approved 测试用例（related_fp = FP_NAME），幂等。"""
    from app.db.models import TestCase
    from sqlalchemy import select
    exist = (await session.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one_or_none()
    if exist:
        exist.status = "approved"
        exist.case_json = {"title": title, "related_fp": FP_NAME}
        return
    session.add(TestCase(
        id=case_id, req_id=req_id, product_id=PRODUCT,
        title=title, type="functional", priority="P1", status="approved",
        case_json={"title": title, "related_fp": FP_NAME}, confidence=1.0,
    ))


async def _cleanup(session, orig_dates):
    from app.db.models import TestCase, FunctionalPointCases, Project
    from sqlalchemy import select, delete
    await session.execute(delete(TestCase).where(TestCase.id.in_([CASE_A, CASE_B])))
    await session.execute(delete(FunctionalPointCases).where(
        FunctionalPointCases.product_id == PRODUCT,
        FunctionalPointCases.fp_name == FP_NAME,
    ))
    for pid, dt in orig_dates.items():
        p = await session.get(Project, pid)
        if p:
            p.go_live_date = dt
    await session.commit()


async def main():
    from app.db.database import async_session
    from app.db.models import Project
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core.auth import get_default_token

    print("=" * 72)
    print("v3.2.13b 功能点最新版用例集 —— 上线时间变更翻转验证")
    print("=" * 72)

    orig_dates = {}
    async with async_session() as s:
        pa = await s.get(Project, PROJ_A)
        pb = await s.get(Project, PROJ_B)
        orig_dates = {PROJ_A: pa.go_live_date, PROJ_B: pb.go_live_date}
        print(f"\n[初始] A={PROJ_A} ver={pa.version} go_live={pa.go_live_date}")
        print(f"[初始] B={PROJ_B} ver={pb.version} go_live={pb.go_live_date}")
        print(f"[初始] functional_point_cases 记录: {'存在' if await _fpc_row(s) else '不存在'}")

        await _ensure_case(s, CASE_A, REQ_A, "E2E-A: 权限回收记录校验")
        await _ensure_case(s, CASE_B, REQ_B, "E2E-B: 权限回收记录校验")
        await s.commit()
        print(f"[准备] 已插入 A/B 两条 approved 用例（related_fp={FP_NAME}）")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers.update({"Authorization": f"Bearer {get_default_token()}"})

        # ---- 步骤 1：B 的上线时间更新 → 用例集应归属 B ----
        print(f"\n[步骤1] 设置 B 上线时间 = {DATE_B}")
        r1 = await client.patch(f"/api/products/projects/{PROJ_B}/go-live-date",
                                json={"go_live_date": DATE_B, "reason": "E2E 步骤1"})
        d1 = r1.json().get("data", {})
        print(f"  HTTP {r1.status_code}, fp_case_sets_refreshed={d1.get('fp_case_sets_refreshed')}")

        async with async_session() as s:
            row = await _fpc_row(s)
            step1_ver = row.project_version if row else None
            step1_date = row.go_live_date if row else None
            print(f"  用例集归属: version={step1_ver}, go_live={step1_date}")
            ok1 = step1_ver == "2.1"

        # ---- 步骤 2：A 的上线时间更新到更晚 → 用例集应翻转为 A ----
        print(f"\n[步骤2] 设置 A 上线时间 = {DATE_A}（晚于 B）")
        r2 = await client.patch(f"/api/products/projects/{PROJ_A}/go-live-date",
                                json={"go_live_date": DATE_A, "reason": "E2E 步骤2"})
        d2 = r2.json().get("data", {})
        print(f"  HTTP {r2.status_code}, fp_case_sets_refreshed={d2.get('fp_case_sets_refreshed')}")

        async with async_session() as s:
            row = await _fpc_row(s)
            step2_ver = row.project_version if row else None
            step2_date = row.go_live_date if row else None
            step2_cases = len(row.content or []) if row else 0
            step2_ids = list(row.source_case_ids or []) if row else []
            print(f"  用例集归属: version={step2_ver}, go_live={step2_date}, 用例数={step2_cases}")
            print(f"  证据链 case_ids={step2_ids}")
            # 翻转成功：归属从 B(2.1) 变为 A(2.1c)，且收录的是 case A
            ok2 = step2_ver == "2.1c" and CASE_A in step2_ids

    # ---- 清理 ----
    async with async_session() as s:
        await _cleanup(s, orig_dates)
        print("\n[清理] 测试用例/用例集记录已删除，项目上线时间已还原")

    print("\n" + "=" * 72)
    print("验证汇总")
    print("=" * 72)
    print(f"  步骤1 归属 B(2.1)     : {'PASS' if ok1 else 'FAIL'}")
    print(f"  步骤2 翻转归属 A(2.1c): {'PASS' if ok2 else 'FAIL'}")
    print(f"\n  总结果: {'ALL PASS' if (ok1 and ok2) else 'HAS FAILURES'}")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
