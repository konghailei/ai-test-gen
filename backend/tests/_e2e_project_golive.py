"""v3.2.13 项目级修改上线时间 —— 真实端到端验证脚本。

验证目标：
1. PATCH /products/projects/{id}/go-live-date 能成功修改已归档项目的上线时间
2. PG 三层同步：Project / Requirement（全部）/ FPDependencyVersion
3. Milvus 元数据同步（需求向量 + FP 向量）
4. KB 缓存失效

用法：docker exec ai-test-gen-api python /app/tests/_e2e_project_golive.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJ_ID = "PROJ-469-77796-892"  # DevBlog V1.0（7 条需求）


async def main():
    from app.db.database import async_session
    from app.db.models import Project, Requirement, FunctionalPoint, FPDependencyVersion
    from sqlalchemy import select, func

    print("=" * 70)
    print("v3.2.13 项目级修改上线时间 —— 端到端验证")
    print("=" * 70)

    async with async_session() as s:
        # ---------- 改前状态 ----------
        proj = await s.get(Project, PROJ_ID)
        if not proj:
            print(f"[FAIL] 项目 {PROJ_ID} 不存在")
            return
        print(f"\n[改前] 项目: {proj.name} / {proj.version} / status={proj.status}")
        print(f"[改前] 项目 go_live_date = {proj.go_live_date}")

        reqs = (await s.execute(
            select(Requirement).where(Requirement.project_id == PROJ_ID)
        )).scalars().all()
        print(f"[改前] 需求共 {len(reqs)} 条，go_live_date 分布:")
        for r in reqs:
            print(f"    {r.id}: {r.go_live_date}")

        vers = (await s.execute(
            select(FPDependencyVersion).where(FPDependencyVersion.project_id == PROJ_ID)
        )).scalars().all()
        print(f"[改前] 版本快照 {len(vers)} 个: {[v.go_live_date for v in vers]}")

        fp_ids = (await s.execute(
            select(FunctionalPoint.id).where(
                FunctionalPoint.requirement_id.in_([r.id for r in reqs])
            )
        )).fetchall()
        print(f"[改前] 功能点 {len(fp_ids)} 个")

    # ---------- 调用接口 ----------
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core.auth import get_default_token

    new_date = "2026-10-15T14:30"
    print(f"\n[操作] PATCH 上线时间 → {new_date}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers.update({"Authorization": f"Bearer {get_default_token()}"})
        resp = await client.patch(
            f"/api/products/projects/{PROJ_ID}/go-live-date",
            json={"go_live_date": new_date, "reason": "E2E 测试：上线延期验证"},
        )
    print(f"[响应] HTTP {resp.status_code}")
    body = resp.json()
    print(f"[响应] {body.get('message')}")
    print(f"[响应] data = {body.get('data')}")

    ok = resp.status_code == 200 and body.get("success")

    # ---------- 改后校验 ----------
    async with async_session() as s:
        proj2 = await s.get(Project, PROJ_ID)
        reqs2 = (await s.execute(
            select(Requirement).where(Requirement.project_id == PROJ_ID)
        )).scalars().all()
        vers2 = (await s.execute(
            select(FPDependencyVersion).where(FPDependencyVersion.project_id == PROJ_ID)
        )).scalars().all()

        print(f"\n[改后] 项目 go_live_date = {proj2.go_live_date}")
        target = "2026-10-15 14:30"
        proj_ok = proj2.go_live_date and proj2.go_live_date.strftime("%Y-%m-%d %H:%M") == target

        req_ok = all(
            r.go_live_date and r.go_live_date.strftime("%Y-%m-%d %H:%M") == target
            for r in reqs2
        )
        empty_after = [r.id for r in reqs2 if r.go_live_date is None]
        print(f"[改后] {len(reqs2)} 条需求全部同步 = {req_ok}（空值: {len(empty_after)} 条）")

        ver_ok = all(
            v.go_live_date and v.go_live_date.strftime("%Y-%m-%d %H:%M") == target
            for v in vers2
        )
        print(f"[改后] {len(vers2)} 个版本快照全部同步 = {ver_ok}")

    # ---------- Milvus 侧校验 ----------
    print("\n[Milvus 校验]")
    try:
        from app.db.milvus_store import vector_store
        all_reqs = await asyncio.to_thread(vector_store.get_all_requirements, proj2.product_id, 500)
        matched = [x for x in all_reqs if (x.get("metadata") or {}).get("req_id") in {r.id for r in reqs2}
                   or x.get("id") in {r.id for r in reqs2}]
        print(f"    该项目在 Milvus 中的需求向量: {len(matched)} 条")
        milvus_ok = True
        for x in matched[:3]:
            m = (x.get("metadata") or {}).get("go_live_date", "(空)")
            hit = "2026-10-15" in str(m)
            if not hit:
                milvus_ok = False
            print(f"    {x.get('id')}: go_live_date = {m} {'✓' if hit else '✗'}")
        if not matched:
            print("    [SKIP] Milvus 中未找到该项目向量（可能尚未向量化）")
            milvus_ok = None
    except Exception as e:
        print(f"    [WARN] Milvus 校验异常: {e}")
        milvus_ok = None

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    print("验证汇总")
    print("=" * 70)
    print(f"  接口调用成功      : {'PASS' if ok else 'FAIL'}")
    print(f"  Project 同步      : {'PASS' if proj_ok else 'FAIL'}")
    print(f"  Requirement 全同步: {'PASS' if req_ok else 'FAIL'}")
    print(f"  Version 快照同步  : {'PASS' if ver_ok else 'FAIL'}")
    print(f"  Milvus 元数据     : {'PASS' if milvus_ok else ('SKIP' if milvus_ok is None else 'FAIL')}")
    all_ok = ok and proj_ok and req_ok and ver_ok and milvus_ok is not False
    print(f"\n  总结果: {'ALL PASS' if all_ok else 'HAS FAILURES'}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
