"""检查任务详情和功能点生成情况"""
import asyncio
import json
from app.db.database import get_session
from app.db.models import AnalysisTask, Requirement
from sqlalchemy import select


async def main():
    async for session in get_session():
        # 1. 查询所有任务
        stmt = (
            select(AnalysisTask)
            .where(AnalysisTask.req_id == "REQ-1784855029523-4306")
            .order_by(AnalysisTask.created_at.desc())
        )
        result = await session.execute(stmt)
        tasks = result.scalars().all()

        print("=" * 80)
        print("所有任务：")
        for t in tasks:
            print(f"\n  task_id={t.id}")
            print(f"  task_type={t.task_type}")
            print(f"  status={t.status}")
            print(f"  phase={t.phase}")
            print(f"  created_at={t.created_at}")
            print(f"  completed_at={t.completed_at}")
            print(f"  error={t.error[:200] if t.error else 'None'}")
            rj = t.result_json or {}
            if isinstance(rj, dict):
                print(f"  result_json keys: {list(rj.keys())}")
                if "questions" in rj:
                    qs = rj["questions"] or []
                    print(f"  questions count: {len(qs)}")
                if "fps" in rj:
                    fps = rj["fps"] or []
                    print(f"  fps count: {len(fps)}")
                    for fp in fps[:3]:
                        print(f"    - {fp.get('name', '') if isinstance(fp, dict) else fp}")
                if "functional_points" in rj:
                    fps = rj["functional_points"] or []
                    print(f"  functional_points count: {len(fps)}")
                if "srr" in rj:
                    srr = rj["srr"] or {}
                    fps = srr.get("functional_points") or srr.get("fps") or []
                    print(f"  srr.functional_points count: {len(fps)}")

        # 2. 检查 task_type 枚举值
        print("\n" + "=" * 80)
        print("检查 task_type 值（用于 qa_records 回找）：")
        stmt2 = (
            select(AnalysisTask.task_type)
            .where(AnalysisTask.req_id == "REQ-1784855029523-4306")
            .distinct()
        )
        result2 = await session.execute(stmt2)
        types = result2.scalars().all()
        print(f"  所有 task_type 值: {types}")
        print(f"  回找逻辑查找的是: 'upload_multi'")
        print(f"  匹配: {'upload_multi' in types}")

        # 3. 检查 requirement 的 srr_json 中功能点
        req = await session.get(Requirement, "REQ-1784855029523-4306")
        if req:
            srr = req.srr_json or {}
            fps = srr.get("functional_points") or srr.get("fps") or []
            print(f"\n  当前 SRR 功能点数量: {len(fps)}")
            fp_index = srr.get("fp_index", [])
            print(f"  fp_index 数量: {len(fp_index)}")
            fp_summary = srr.get("fp_summary", [])
            print(f"  fp_summary 数量: {len(fp_summary)}")
            if fp_summary:
                for fp in fp_summary:
                    print(f"    - {fp.get('name', '') if isinstance(fp, dict) else fp}")

        break


if __name__ == "__main__":
    asyncio.run(main())
