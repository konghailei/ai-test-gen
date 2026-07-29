"""检查 SRR 生成质量"""
import asyncio, json
from app.db.database import async_session
from app.db.models import Requirement, FunctionalPoint
from sqlalchemy import select

async def check():
    async with async_session() as s:
        r = await s.execute(
            select(Requirement.srr_json, Requirement.title, Requirement.status).where(Requirement.id == "REQ-1784855029523-4306")
        )
        row = r.first()
        if not row:
            print("NOT FOUND")
            return
        srr, title, status = row
        print(f"Title: {title}")
        print(f"Status: {status}")
        req = srr.get("requirement", {})
        print(f"\n=== 总体SRR ===")
        print(f"  title: {req.get('title','')[:60]}")
        print(f"  priority: {req.get('priority','')}")
        print(f"  modules: {req.get('modules',[])}")
        print(f"  summary: {req.get('summary','')[:150]}")
        # 流程
        flows = srr.get("flows", {}) or {}
        hp = flows.get("happy_path", {}) or {}
        steps = hp.get("steps", []) or []
        print(f"\n  主流程 steps: {len(steps)}")
        for st in steps[:3]:
            print(f"    {st.get('order','')}. {st.get('action','')[:50]} -> {st.get('expect','')[:50]}")
        alt = flows.get("alternate_flows", []) or []
        print(f"  异常分支: {len(alt)}")
        # 规则
        for f in ["business_rules", "boundary_conditions", "data_constraints", "acceptance_criteria", "preconditions"]:
            v = srr.get(f, [])
            print(f"  {f}: {len(v) if isinstance(v, list) else 'N/A'}")
        # 功能点
        fps = srr.get("functional_points", []) or srr.get("fps", []) or []
        print(f"\n  SRR内 functional_points: {len(fps)}")

        # 查 DB 中 FunctionalPoint
        r2 = await s.execute(
            select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type, FunctionalPoint.srr_json)
            .where(FunctionalPoint.requirement_id == "REQ-1784855029523-4306")
            .order_by(FunctionalPoint.created_at)
        )
        fp_rows = r2.all()
        print(f"\n=== 功能点 (DB: {len(fp_rows)} 个) ===")
        for i, fp in enumerate(fp_rows):
            fp_srr = fp[3] or {}
            print(f"\n  FP[{i}]: {fp[1]} (type={fp[2]})")
            print(f"    sub_srr keys: {list(fp_srr.keys())}")
            summary = fp_srr.get("summary") or (fp_srr.get("requirement") or {}).get("summary", "")
            print(f"    summary: {str(summary)[:80]}")
            fp_flows = fp_srr.get("flows", {}) or {}
            fp_steps = (fp_flows.get("happy_path") or {}).get("steps", []) or []
            print(f"    主流程 steps: {len(fp_steps)}")
            fp_alt = fp_flows.get("alternate_flows", []) or []
            print(f"    异常分支: {len(fp_alt)}")
            for f in ["business_rules", "data_constraints", "acceptance_criteria", "non_functional", "boundary_conditions", "preconditions", "ui_rules", "data_model", "assumptions", "test_points_hint"]:
                v = fp_srr.get(f)
                if v:
                    cnt = len(v) if isinstance(v, (list, dict)) else 1
                    print(f"    {f}: {cnt}")

asyncio.run(check())
