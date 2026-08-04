"""在 Docker 容器内运行：检查 REQ-1784855029523-4306 的状态"""
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

        srr = req.srr_json or {}
        print(f"\nSRR 顶层字段: {list(srr.keys())}")

        # 澄清历史（存在 srr_json._clarify_history 中）
        clarify_history = srr.get("_clarify_history", []) or []
        print(f"\n澄清历史条数: {len(clarify_history)}")
        for i, c in enumerate(clarify_history):
            print(f"  [{i}] timestamp={c.get('timestamp', '')} action={c.get('action', '')}")
            if c.get("qa_records"):
                print(f"      qa_records 数量: {len(c['qa_records'])}")
                for qa in c["qa_records"][:5]:
                    print(f"        - id={qa.get('id', '')[:30]} field={qa.get('field', '')}")
                    print(f"          Q: {(qa.get('question', '') or '')[:100]}")
                    print(f"          A: {(qa.get('answer', '') or '')[:100]}")
            elif c.get("questions"):
                print(f"      questions 数量: {len(c['questions'])} (无 qa_records)")
                for q in c["questions"][:3]:
                    print(f"        - id={q.get('id', '')[:30]} field={q.get('field', '')}")
            else:
                print(f"      ⚠️ 既无 qa_records 也无 questions")

        # 澄清状态
        clarify_state = srr.get("_clarify_state", {}) or {}
        cs_questions = clarify_state.get("questions", []) or []
        print(f"\n_clarify_state.questions 数量: {len(cs_questions)}")
        if cs_questions:
            seen_fields = {}
            seen_ids = {}
            for q in cs_questions:
                fid = q.get("id", "")
                field = q.get("field", "")
                seen_fields[field] = seen_fields.get(field, 0) + 1
                seen_ids[fid] = seen_ids.get(fid, 0) + 1
            dup_fields = {k: v for k, v in seen_fields.items() if v > 1}
            dup_ids = {k: v for k, v in seen_ids.items() if v > 1}
            if dup_fields:
                print(f"  ⚠️ 重复 field: {dup_fields}")
            if dup_ids:
                print(f"  ⚠️ 重复 id: {dup_ids}")
            if not dup_fields and not dup_ids:
                print(f"  ✅ 无重复问题")
            for i, q in enumerate(cs_questions):
                print(f"    [{i}] id={q.get('id', '')[:30]} field={q.get('field', '')} Q={(q.get('question', '') or '')[:70]}")

        # SRR 关键字段
        req_info = srr.get("requirement", {}) or {}
        print(f"\nrequirement.title: {req_info.get('title', '')}")
        print(f"requirement.priority: {req_info.get('priority', '')}")
        print(f"requirement.product_id: {req_info.get('product_id', '')}")

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
                    print(f"      data_constraints: {len(fp_srr.get('data_constraints', []))}")
                    print(f"      acceptance_criteria: {len(fp_srr.get('acceptance_criteria', []))}")

        # 检查最近任务
        print("\n" + "=" * 80)
        print("检查最近任务（含澄清问题）：")
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
            print(f"\n  task_id={t.id} type={t.task_type} status={t.status} phase={t.phase}")
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
                        print(f"      [{i}] id={q.get('id', '')[:30]} field={q.get('field', '')} Q={(q.get('question', '') or '')[:70]}")
        break


if __name__ == "__main__":
    asyncio.run(main())
