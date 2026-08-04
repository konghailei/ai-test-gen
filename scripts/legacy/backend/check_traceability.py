"""查询 Traceability 追溯表与导出文件验证。"""

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import text
from app.db.database import async_session


async def main():
    async with async_session() as session:
        # ============ 1. Traceability 表 ============
        print("\n" + "=" * 70)
        print("  1. Traceability 追溯矩阵表")
        print("=" * 70)
        tr_rows = (await session.execute(text(
            "SELECT test_case_id, requirement_id, requirement_field, trace_type, confidence "
            "FROM traceability "
            "WHERE requirement_id = :rid "
            "ORDER BY id"
        ), {"rid": "REQ-1784473087033-4566"})).all()
        print(f"\n  共 {len(tr_rows)} 条追溯记录")
        if tr_rows:
            fp_tags = [r.requirement_field for r in tr_rows if r.requirement_field and r.requirement_field.startswith("fp:")]
            print(f"  其中 FP 追溯记录: {len(fp_tags)} 条")
            fp_dist = Counter(fp_tags)
            print(f"  FP 分布: {dict(fp_dist)}")
            print(f"\n  样例（前 5 条）:")
            for i, t in enumerate(tr_rows[:5], 1):
                print(f"  {i}. case={t.test_case_id} field={t.requirement_field} type={t.trace_type} conf={t.confidence:.3f}")

        # ============ 2. 测试用例的 case_json.requirement_field ============
        print("\n" + "=" * 70)
        print("  2. 测试用例 case_json.requirement_field 字段检查")
        print("=" * 70)
        case_rows = (await session.execute(text(
            "SELECT id, case_json "
            "FROM test_cases "
            "WHERE req_id = :rid "
            "ORDER BY id LIMIT 5"
        ), {"rid": "REQ-1784473087033-4566"})).all()
        for c in case_rows:
            cj = c.case_json or {}
            rf = cj.get("requirement_field", "(无)") if isinstance(cj, dict) else "(无效)"
            related_fp = cj.get("related_fp", "(无)") if isinstance(cj, dict) else "(无效)"
            print(f"  - {c.id}: requirement_field={rf}, related_fp={related_fp}")

        # ============ 3. 按批次分组用例 ============
        print("\n" + "=" * 70)
        print("  3. 按批次分组用例")
        print("=" * 70)
        batch_rows = (await session.execute(text(
            "SELECT gen_batch_id, COUNT(*) AS cnt, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM test_cases "
            "WHERE req_id = :rid "
            "GROUP BY gen_batch_id "
            "ORDER BY first_at"
        ), {"rid": "REQ-1784473087033-4566"})).all()
        for b in batch_rows:
            print(f"  - 批次 {b.gen_batch_id}: {b.cnt} 条 ({b.first_at} → {b.last_at})")

        # ============ 4. 用例类型与状态分布 ============
        print("\n" + "=" * 70)
        print("  4. 用例状态分布")
        print("=" * 70)
        type_status_rows = (await session.execute(text(
            "SELECT type, status, COUNT(*) AS cnt, AVG(confidence) AS avg_conf "
            "FROM test_cases "
            "WHERE req_id = :rid "
            "GROUP BY type, status "
            "ORDER BY type, status"
        ), {"rid": "REQ-1784473087033-4566"})).all()
        for r in type_status_rows:
            print(f"  - {r.type}/{r.status}: {r.cnt} 条, 平均置信度={r.avg_conf:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
