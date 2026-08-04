"""深入检查 REQ-1785821750443-7544：clarify 阶段为何没有生成澄清问题、为何没有 KB 访问日志、FP modules 为空、
并排查同产品/项目下其它需求的 SRR 中已有 FP，看是否存在"应复用而未复用"的情况。"""
import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db.database import async_session
from app.db.models import Requirement, FunctionalPoint, AnalysisTask
from sqlalchemy import select, or_

REQ_ID = "REQ-1785821750443-7544"


async def pprint(label, obj, head_chars=3000):
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    print(f"\n--- {label} ---")
    if len(s) > head_chars:
        print(s[:head_chars] + f"\n... (剩余 {len(s) - head_chars} 字符省略)")
    else:
        print(s)


async def main():
    async with async_session() as s:
        r = await s.get(Requirement, REQ_ID)
        if not r:
            print("NOT FOUND"); return
        srr = r.srr_json or {}
        cs = srr.get("_clarify_state") or {}

        print("=" * 80)
        print("1. SRR 顶层结构 & 关键字段覆盖度")
        print("=" * 80)
        top_keys = [
            "requirement", "flows", "functional_requirements", "non_functional_requirements",
            "data_constraints", "acceptance_criteria", "cross_fp_flows",
            "fp_contracts", "shared_business_rules", "fp_index", "global_constraints",
            "risk_analysis", "data_model", "modules", "actors",
            "clarify_answers", "_clarify_history", "_kb_query_logs",
        ]
        for k in top_keys:
            v = srr.get(k)
            t = type(v).__name__
            info = ""
            if isinstance(v, list): info = f"len={len(v)}"
            elif isinstance(v, dict): info = f"keys={list(v.keys())[:10]}"
            elif isinstance(v, str): info = f"str(len={len(v)})"
            else: info = f"value={v}" if v else "MISSING"
            print(f"    {k:<35}: {t:<8} {info}")

        # 关键 completeness 维度
        req_sub = srr.get("requirement") or {}
        for k in ["acceptance_criteria", "scope_inclusions", "scope_exclusions", "data_inputs",
                  "data_outputs", "business_rules", "boundary_conditions", "dependencies",
                  "assumptions", "non_functional", "stakeholders", "project_id", "product_id",
                  "modules", "change_type", "priority"]:
            v = req_sub.get(k)
            if isinstance(v, list): has = len(v) > 0
            elif isinstance(v, dict): has = bool(v)
            else: has = bool(v)
            print(f"    [requirement] {k:<28}: {'OK' if has else 'MISSING':>8}  type={type(v).__name__}")

        await pprint("2. quality_report（澄清质量报告）", cs.get("quality_report"))
        await pprint("3. auto_augmentations（KB 自动补全条目）", cs.get("auto_augmentations"))
        await pprint("4. field_conflicts（KB 与现有 SRR 字段冲突）", cs.get("field_conflicts"))

        # 读取第 0 轮 clarify 历史（实际第一轮 LLM+KB 交互）
        print("\n" + "=" * 80)
        print("5. 第一轮 clarify 历史（_clarify_history[0]）")
        print("=" * 80)
        ch0 = (srr.get("_clarify_history") or [])[0] if srr.get("_clarify_history") else None
        if ch0:
            for k, v in ch0.items():
                if isinstance(v, list):
                    print(f"    {k:<25}: list len={len(v)}")
                    if k == "qa_records":
                        for i, qr in enumerate(v[:5]):
                            print(f"      [{i}] {json.dumps(qr, ensure_ascii=False)[:200]}")
                elif isinstance(v, dict):
                    print(f"    {k:<25}: dict keys={list(v.keys())[:8]}")
                else:
                    print(f"    {k:<25}: {v!r}"[:120])

        # 读取所有需求的 SRR，看知识库中是否有"编辑器、内容安全"相关 FP
        print("\n" + "=" * 80)
        print("6. 全量需求 & FP：查找 编辑器/内容安全/敏感词/举报/草稿/素材库 相关 FP")
        print("=" * 80)
        all_reqs = (await s.execute(select(Requirement))).scalars().all()
        print(f"    数据库总需求数: {len(all_reqs)}")
        keywords = ["编辑", "素材", "草稿", "版本", "发布", "命令", "专注", "冲突", "状态栏", "大纲",
                    "敏感词", "举报", "风控", "灌水", "内容安全"]

        fps_all = (await s.execute(select(FunctionalPoint))).scalars().all()
        print(f"    数据库总 FP 数: {len(fps_all)}")
        hit_own = []
        hit_other = []
        for f in fps_all:
            name = (f.name or "").lower()
            any_kw = any(kw in f.name for kw in keywords)
            if f.requirement_id == REQ_ID:
                if any_kw: hit_own.append(f)
            else:
                if any_kw: hit_other.append(f)
        print(f"    当前需求命中关键词的 FP: {len(hit_own)}/{len([x for x in fps_all if x.requirement_id==REQ_ID])}")
        print(f"    其他需求中命中关键词的 FP: {len(hit_other)}")
        for f in hit_other[:60]:
            print(
                f"      - {f.requirement_id:<28}  {f.id}  name={f.name!r:60}  "
                f"type={f.fp_type}  mods={f.modules}  status={f.status}"
            )

        # 7. 检查 AnalysisTask 的 result_json 细节（upload_requirement & submit_clarify）
        print("\n" + "=" * 80)
        print("7. AnalysisTask.result_json 全量展开")
        print("=" * 80)
        tasks = (await s.execute(
            select(AnalysisTask)
            .where(AnalysisTask.req_id == REQ_ID)
            .order_by(AnalysisTask.created_at.asc())
        )).scalars().all()
        for t in tasks:
            print(f"\n  task: type={t.task_type} id={t.id} status={t.status} phase={t.phase}")
            if t.error:
                print(f"    error={(t.error or '')[:400]}")
            rj = t.result_json or {}
            for k, v in rj.items():
                if k == "auto_augmentations":
                    print(f"    {k}: list len={len(v) if isinstance(v, list) else type(v).__name__}")
                    if isinstance(v, list):
                        for i, it in enumerate(v[:10]):
                            print(f"      [{i}] {json.dumps(it, ensure_ascii=False)[:260]}")
                elif k == "clarify_questions" or k == "questions":
                    print(f"    {k}: list len={len(v) if isinstance(v, list) else type(v).__name__}")
                    if isinstance(v, list):
                        for i, it in enumerate(v[:20]):
                            print(f"      [{i}] {json.dumps(it, ensure_ascii=False)[:280]}")
                else:
                    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, (str, int, float, bool)) else str(v)
                    print(f"    {k}: {s[:600]}")

        # 8. KnowledgeQA 表 & 知识相关记录：搜索是否有对应"编辑器/内容安全"相关知识（表中存在 KnowledgeQA）
        print("\n" + "=" * 80)
        print("8. KnowledgeQA 表：关键词命中前20（判断知识库实际内容）")
        print("=" * 80)
        try:
            from app.db.models import KnowledgeQA
            kw_conds = or_(*[KnowledgeQA.question.contains(k) for k in keywords[:10]] +
                           [KnowledgeQA.answer.contains(k) for k in keywords[:10]] +
                           [KnowledgeQA.keywords.contains(k) for k in keywords[:10]])
            rows = (await s.execute(
                select(KnowledgeQA).where(kw_conds).order_by(KnowledgeQA.updated_at.desc()).limit(20)
            )).scalars().all()
            print(f"    命中条数: {len(rows)}")
            for x in rows[:20]:
                print(
                    f"    - [{x.id}] product_id={x.product_id}  module_id={x.module_id}  "
                    f"category={x.category}  status={x.status}  "
                    f"Q[:60]={(x.question or '')[:60]!r}  A[:80]={(x.answer or '')[:80]!r}"
                )
        except Exception as e:
            print(f"    查询 KnowledgeQA 异常: {e!r}")

        # 9. 其它需求 SRR 的 fp_index / FP 名称中关键词命中（使用 SQL text 扫描 content 列）
        print("\n" + "=" * 80)
        print("9. srr_json 中与本需求 keywords 命中的其它需求（判断是否存在相似历史/可复用 FP）")
        print("=" * 80)
        try:
            from sqlalchemy import text
            for kw in ["编辑器", "内容安全", "敏感词", "举报", "风控", "草稿", "素材库"]:
                # 先看标题命中（高效）
                rrows = (await s.execute(text(
                    "SELECT id, title, product_id, project_id, status, created_at "
                    "FROM requirements WHERE id != :rid AND (title ILIKE :kw OR srr_json::text ILIKE :kw) "
                    "ORDER BY created_at DESC LIMIT 8"
                ), {"rid": REQ_ID, "kw": f"%{kw}%"})).all()
                if rrows:
                    print(f"\n    关键词 [{kw}] 命中 {len(rrows)} 条其它需求:")
                    for rr in rrows:
                        print(f"      - {rr.id}  title={rr.title[:80]!r}  product={rr.product_id}  "
                              f"project={rr.project_id}  status={rr.status}  created={rr.created_at}")
        except Exception as e:
            print(f"    SQL 模糊查询异常: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
