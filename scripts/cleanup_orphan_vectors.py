"""一次性清理脚本：清理 Milvus 中遗留的需求与用例向量。

背景：
    v2.8.51 之前 delete_requirement / delete_test_case 未调用 flush，
    导致删除产品/项目/需求后向量遗留。此脚本对照 PostgreSQL 中现存
    的 product_id 列表，删除 Milvus 中所有不属于这些产品的向量。

使用方法：
    docker exec ai-test-gen-api python /app/cleanup_orphan_vectors.py

执行后会输出：
    - 清理前 PostgreSQL 与 Milvus 中各产品的需求数对比
    - 清理的遗留向量详情
    - 清理后 Milvus 中需求数
"""
import asyncio
import sys
from collections import Counter

sys.path.insert(0, '/app')

from sqlalchemy import select
from app.db.database import async_session
from app.db.models import Requirement, TestCase, Product
from app.db.milvus_store import vector_store


async def cleanup() -> None:
    # 1) 查 PostgreSQL 中现存的 product_id 集合（products 表 + requirements 表兜底）
    async with async_session() as session:
        prod_result = await session.execute(select(Product.id))
        pg_product_ids: set[str] = {r[0] for r in prod_result.all()}

        req_result = await session.execute(select(Requirement.id, Requirement.product_id))
        pg_req_rows = req_result.all()
        pg_req_ids: set[str] = {r[0] for r in pg_req_rows}
        # 兜底：requirements 表中出现的 product_id 也算现存（即使 products 表漏了）
        for r in pg_req_rows:
            if r[1]:
                pg_product_ids.add(r[1])

        tc_result = await session.execute(select(TestCase.id, TestCase.product_id))
        pg_tc_ids: set[str] = {r[0] for r in tc_result.all()}
        for r in tc_result.all():
            if r[1]:
                pg_product_ids.add(r[1])

    print(f"[PostgreSQL] 现存产品数: {len(pg_product_ids)} → {sorted(pg_product_ids)}")
    print(f"[PostgreSQL] 现存需求数: {len(pg_req_ids)}")
    print(f"[PostgreSQL] 现存用例数: {len(pg_tc_ids)}")

    # 2) 查 Milvus 中的所有需求与用例向量
    mv_reqs = vector_store.get_all_requirements(limit=500)
    mv_cases = vector_store.get_all_test_cases(limit=2000)

    print()
    print(f"[Milvus] 需求向量总数: {len(mv_reqs)}")
    print(f"[Milvus] 用例向量总数: {len(mv_cases)}")

    mv_req_pid_count = Counter(r['metadata'].get('product_id', '') for r in mv_reqs)
    print(f"[Milvus] 需求向量按产品分布:")
    for pid, n in mv_req_pid_count.most_common():
        marker = "✓" if pid in pg_product_ids else "✗(遗留)"
        print(f"    {pid}: {n} {marker}")

    # 3) 找出遗留向量（不属于任何现存产品）
    orphan_reqs = [r for r in mv_reqs if r['metadata'].get('product_id', '') not in pg_product_ids]
    orphan_cases = [c for c in mv_cases if c['metadata'].get('product_id', '') not in pg_product_ids]
    print()
    print(f"[诊断] 遗留需求向量: {len(orphan_reqs)} 条")
    print(f"[诊断] 遗留用例向量: {len(orphan_cases)} 条")

    if not orphan_reqs and not orphan_cases:
        print()
        print("[完成] 无遗留向量，无需清理。")
        return

    # 4) 删除遗留向量（按 product_id 批量）
    orphan_req_pids = {r['metadata'].get('product_id', '') for r in orphan_reqs}
    orphan_case_pids = {c['metadata'].get('product_id', '') for c in orphan_cases}

    print()
    print("[清理] 开始删除遗留向量...")
    total_deleted_reqs = 0
    total_deleted_cases = 0
    for pid in orphan_req_pids:
        if not pid:
            continue
        n = vector_store.delete_requirement_by_product(pid)
        total_deleted_reqs += n
        print(f"    删除需求向量: product={pid}, count={n}")

    for pid in orphan_case_pids:
        if not pid:
            continue
        n = vector_store.delete_test_case_by_product(pid)
        total_deleted_cases += n
        print(f"    删除用例向量: product={pid}, count={n}")

    # 5) 验证
    print()
    print("[验证] 重新查询 Milvus...")
    mv_reqs_after = vector_store.get_all_requirements(limit=500)
    mv_cases_after = vector_store.get_all_test_cases(limit=2000)
    print(f"[验证] Milvus 需求向量: {len(mv_reqs)} → {len(mv_reqs_after)} (清理 {len(mv_reqs) - len(mv_reqs_after)})")
    print(f"[验证] Milvus 用例向量: {len(mv_cases)} → {len(mv_cases_after)} (清理 {len(mv_cases) - len(mv_cases_after)})")

    mv_req_pid_count_after = Counter(r['metadata'].get('product_id', '') for r in mv_reqs_after)
    print(f"[验证] 清理后需求向量按产品分布:")
    for pid, n in mv_req_pid_count_after.most_common():
        marker = "✓" if pid in pg_product_ids else "✗(异常)"
        print(f"    {pid}: {n} {marker}")

    print()
    print(f"[完成] 共清理遗留需求向量 {total_deleted_reqs} 条，用例向量 {total_deleted_cases} 条。")


if __name__ == "__main__":
    asyncio.run(cleanup())
