"""检查 REQ-1784855029523-4306 的状态：SRR质量、澄清历史、问题重复性"""
import asyncio
import json
from app.db.database import get_session
from app.db.models import Requirement, AnalysisTask
from sqlalchemy import select


async def main():
    async for session in get_session():
        req = await session.get(Requirement, "REQ-1784855029523-4306")
        if not req:
            print("❌ 需求不存在")
            return

        print("=" * 80)
        print(f"需求ID: {req.id}")
        print(f"标题: {req.title}")
        print(f"状态: {req.status}")
        print(f"版本: {req.version}")
        print(f"产品ID: {req.product_id}")

        # 澄清历史
        clarifications = req.clarifications or []
        print(f"\n澄清历史条数: {len(clarifications)}")
        for i, c in enumerate(clarifications):
            print(f"  [{i}] timestamp={c.get('timestamp', '')} action={c.get('action', '')}")
            if c.get("qa_records"):
                print(f"      qa_records 数量: {len(c['qa_records'])}")
                for qa in c["qa_records"][:3]:
                    print(f"        - id={qa.get('id', '')[:20]} field={qa.get('field', '')}")
                    print(f"          Q: {(qa.get('question', '') or '')[:80]}")
                    print(f"          A: {(qa.get('answer', '') or '')[:80]}")
            elif c.get("questions"):
                print(f"      questions 数量: {len(c['questions'])}")
                for q in c["questions"][:3]:
                    print(f"        - id={q.get('id', '')[:20]} field={q.get('field', '')}")

        # 答案
        answers = req.answers or {}
        print(f"\n答案数量: {len(answers)}")
        for k, v in list(answers.items())[:5]:
            print(f"  {k}: {v[:80] if isinstance(v, str) else v}")

        # SRR 关键字段
        srr = req.srr_json or {}
        print(f"\nSRR 顶层字段: {list(srr.keys())}")
        req_info = srr.get("requirement", {}) or {}
        print(f"  requirement.title: {req_info.get('title', '')}")
        print(f"  requirement.priority: {req_info.get('priority', '')}")

        # 总SRR
        flows = srr.get("flows", {}) or {}
        hp = flows.get("happy_path", {}) or {}
        print(f"\n[总体SRR] happy_path.steps: {len(hp.get('steps', []))}")
        print(f"[总体SRR] alternate_flows: {len(flows.get('alternate_flows', []))}")
        print(f"[总体SRR] business_rules: {len(srr.get('business_rules', []))}")
        print(f"[总体SRR] acceptance_criteria: {len(srr.get('acceptance_criteria', []))}")

        # 功能点
        fps = srr.get("functional_points") or srr.get("fps") or []
        print(f"\n功能点数量: {len(fps)}")
        for i, fp in enumerate(fps):
            if isinstance(fp, dict):
                print(f"  [{i}] name={fp.get('name', '')}")
                fp_srr = fp.get("srr_json") or fp.get("srr") or {}
                if fp_srr:
                    print(f"      srr 字段: {list(fp_srr.keys())}")
                    fp_flows = fp_srr.get("flows", {}) or {}
                    print(f"      flows.happy_path.steps: {len((fp_flows.get('happy_path', {}) or {}).get('steps', []))}")
                    print(f"      business_rules: {len(fp_srr.get('business_rules', []))}")

        # 检查重复问题
        print("\n" + "=" * 80)
        print("检查澄清问题重复性：")
        # 查询最近一次 clarify 任务
        stmt = (
            select(AnalysisTask)
            .where(AnalysisTask.req_id == req.id)
            .order_by(AnalysisTask.created_at.desc())
            .limit(5)
        )
        result = await session.execute(stmt)
        tasks = result.scalars().all()
        print(f"最近任务数: {len(tasks)}")
        for t in tasks:
            print(f"  task_id={t.id} type={t.task_type} status={t.status} phase={t.phase}")
            rj = t.result_json or {}
            if isinstance(rj, dict):
                questions = rj.get("questions") or []
                if questions:
                    print(f"    questions 数量: {len(questions)}")
                    seen_fields = {}
                    seen_ids = {}
                    for q in questions:
                        fid = q.get("id", "")
                        field = q.get("field", "")
                        seen_fields[field] = seen_fields.get(field, 0) + 1
                        seen_ids[fid] = seen_ids.get(fid, 0) + 1
                    dup_fields = {k: v for k, v in seen_fields.items() if v > 1}
                    dup_ids = {k: v for k, v in seen_ids.items() if v > 1}
                    if dup_fields:
                        print(f"    ⚠️ 重复 field: {dup_fields}")
                    if dup_ids:
                        print(f"    ⚠️ 重复 id: {dup_ids}")
                    if not dup_fields and not dup_ids:
                        print(f"    ✅ 无重复问题")
                    for i, q in enumerate(questions):
                        print(f"      [{i}] id={q.get('id', '')[:20]} field={q.get('field', '')} Q={(q.get('question', '') or '')[:60]}")
        break


if __name__ == "__main__":
    asyncio.run(main())
