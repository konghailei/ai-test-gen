"""检查功能点表"""
import asyncio
from app.db.database import get_session
from app.db.models import FunctionalPoint
from sqlalchemy import select


async def main():
    async for session in get_session():
        stmt = select(FunctionalPoint).where(FunctionalPoint.requirement_id == "REQ-1784855029523-4306")
        result = await session.execute(stmt)
        fps = result.scalars().all()
        print(f"功能点表中的数量: {len(fps)}")
        for fp in fps:
            print(f"\n  fp_id={fp.id}")
            print(f"  name={fp.name}")
            print(f"  fp_type={fp.fp_type}")
            print(f"  status={fp.status}")
            print(f"  srr_json keys: {list((fp.srr_json or {}).keys())}")
            srr = fp.srr_json or {}
            if srr:
                flows = srr.get("flows", {}) or {}
                print(f"  flows.happy_path.steps: {len((flows.get('happy_path', {}) or {}).get('steps', []))}")
                print(f"  business_rules: {len(srr.get('business_rules', []))}")
                print(f"  data_constraints: {len(srr.get('data_constraints', []))}")
                print(f"  acceptance_criteria: {len(srr.get('acceptance_criteria', []))}")
        break


if __name__ == "__main__":
    asyncio.run(main())
