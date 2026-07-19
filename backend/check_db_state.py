"""查询端到端测试结果的数据库状态。

确认需求、功能点、测试用例、生成批次的实际持久化情况。
"""

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy import text
from app.db.database import async_session


async def main():
    async with async_session() as session:
        # ============ 1. 查询测试需求 ============
        print("\n" + "=" * 70)
        print("  1. 查询测试需求（电商订单管理系统）")
        print("=" * 70)
        req_rows = (await session.execute(text(
            "SELECT id, title, status, priority, version, product_id, srr_json, "
            "created_at, updated_at "
            "FROM requirements "
            "WHERE title LIKE '%电商订单%' OR title LIKE '%订单管理%' "
            "ORDER BY created_at DESC LIMIT 5"
        ))).all()
        for r in req_rows:
            srr = r.srr_json or {}
            req_part = srr.get("requirement", {}) if isinstance(srr, dict) else {}
            flows = srr.get("flows", {}) if isinstance(srr, dict) else {}
            hp = flows.get("happy_path", {}) if isinstance(flows, dict) else {}
            hp_steps = hp.get("steps", []) if isinstance(hp, dict) else []
            modules = req_part.get("modules", [])
            dc = srr.get("data_constraints", []) if isinstance(srr, dict) else []
            ac = srr.get("acceptance_criteria", []) if isinstance(srr, dict) else []

            print(f"\n  ID: {r.id}")
            print(f"  标题: {r.title}")
            print(f"  状态: {r.status} | 优先级: {r.priority} | 版本: v{r.version}")
            print(f"  产品: {r.product_id}")
            print(f"  模块数: {len(modules)} | happy_path 步骤数: {len(hp_steps)}")
            print(f"  data_constraints: {len(dc)} | acceptance_criteria: {len(ac)}")
            print(f"  创建: {r.created_at} | 更新: {r.updated_at}")

        if not req_rows:
            print("  ⚠️  未找到测试需求！")
            return

        test_req_id = req_rows[0].id

        # ============ 2. 查询功能点 ============
        print("\n" + "=" * 70)
        print(f"  2. 查询功能点 (req_id={test_req_id})")
        print("=" * 70)
        fp_rows = (await session.execute(text(
            "SELECT id, name, fp_type, status, test_hints "
            "FROM functional_points "
            "WHERE requirement_id = :rid "
            "ORDER BY id"
        ), {"rid": test_req_id})).all()
        print(f"\n  共 {len(fp_rows)} 个功能点:")
        for i, fp in enumerate(fp_rows, 1):
            hints = fp.test_hints if isinstance(fp.test_hints, list) else []
            print(f"  {i}. [{fp.id}] {fp.name}")
            print(f"     类型={fp.fp_type}, 状态={fp.status}, 测试提示={len(hints)} 条")

        # ============ 3. 查询测试用例 ============
        print("\n" + "=" * 70)
        print(f"  3. 查询测试用例 (req_id={test_req_id})")
        print("=" * 70)
        case_rows = (await session.execute(text(
            "SELECT id, title, type, priority, status, confidence, source, gen_batch_id, "
            "case_json, created_at "
            "FROM test_cases "
            "WHERE req_id = :rid "
            "ORDER BY id"
        ), {"rid": test_req_id})).all()
        print(f"\n  共 {len(case_rows)} 条用例")
        if case_rows:
            type_dist = Counter(r.type for r in case_rows)
            priority_dist = Counter(r.priority for r in case_rows)
            status_dist = Counter(r.status for r in case_rows)
            print(f"\n  类型分布: {dict(type_dist)}")
            print(f"  优先级分布: {dict(priority_dist)}")
            print(f"  状态分布: {dict(status_dist)}")
            confs = [r.confidence for r in case_rows if r.confidence is not None]
            if confs:
                print(f"  置信度: min={min(confs):.3f} max={max(confs):.3f} avg={sum(confs)/len(confs):.3f}")

            steps_list = []
            for r in case_rows:
                cj = r.case_json or {}
                steps = cj.get("steps", []) if isinstance(cj, dict) else []
                steps_list.append(len(steps) if isinstance(steps, list) else 0)
            print(f"  步骤数: min={min(steps_list)} max={max(steps_list)} avg={sum(steps_list)/len(steps_list):.2f}")

            ids = [r.id for r in case_rows]
            dup_ids = [i for i, c in Counter(ids).items() if c > 1]
            print(f"  ID 唯一性: {'✓ 无重复' if not dup_ids else f'✗ 重复 ID: {dup_ids}'}")

            fp_tagged = 0
            for r in case_rows:
                cj = r.case_json or {}
                rf = cj.get("requirement_field", "") if isinstance(cj, dict) else ""
                if rf and str(rf).startswith("fp:"):
                    fp_tagged += 1
            print(f"  FP 追溯: {fp_tagged}/{len(case_rows)} 用例标注了 requirement_field=fp:*")

            print(f"\n  批次 ID: {case_rows[0].gen_batch_id}")
            print(f"\n  用例清单（前 10 条）:")
            for i, c in enumerate(case_rows[:10], 1):
                cj = c.case_json or {}
                rf = cj.get("requirement_field", "-") if isinstance(cj, dict) else "-"
                steps = cj.get("steps", []) if isinstance(cj, dict) else []
                print(f"  {i:2d}. [{c.id}] [{c.type}] [{c.priority}] conf={c.confidence:.3f} steps={len(steps)} FP={rf}")
                print(f"      {c.title[:80]}")

        # ============ 4. 查询生成批次 ============
        print("\n" + "=" * 70)
        print(f"  4. 查询生成批次 (req_id={test_req_id})")
        print("=" * 70)
        batch_rows = (await session.execute(text(
            "SELECT id, total_cases, accepted_cases, rejected_cases, "
            "model_used, prompt_version, total_tokens, total_cost, duration_ms, channel, created_at "
            "FROM generation_batches "
            "WHERE req_id = :rid "
            "ORDER BY created_at DESC LIMIT 5"
        ), {"rid": test_req_id})).all()
        for b in batch_rows:
            print(f"\n  批次: {b.id}")
            print(f"  用例总数: {b.total_cases} | 接受: {b.accepted_cases} | 拒绝: {b.rejected_cases}")
            print(f"  模型: {b.model_used} | Prompt 版本: {b.prompt_version} | 通道: {b.channel}")
            print(f"  Token: {b.total_tokens} | 成本: ${b.total_cost:.4f} | 耗时: {b.duration_ms} ms")
            print(f"  创建: {b.created_at}")

        # ============ 5. 查询 KB 回流日志 ============
        print("\n" + "=" * 70)
        print(f"  5. 查询 KB 检索日志 (req_id={test_req_id})")
        print("=" * 70)
        try:
            log_rows = (await session.execute(text(
                "SELECT id, log_type, query_text, match_count, top_sim, created_at "
                "FROM kb_query_logs "
                "WHERE req_id = :rid "
                "ORDER BY created_at DESC LIMIT 10"
            ), {"rid": test_req_id})).all()
            print(f"\n  共 {len(log_rows)} 条 KB 日志:")
            for log in log_rows:
                q = (log.query_text or "")[:60]
                print(f"  - [{log.log_type}] hits={log.match_count} sim={log.top_sim} q='{q}...' @ {log.created_at}")
        except Exception as e:
            print(f"  ⚠️  KB 日志查询失败: {e}")

        # ============ 6. 查询分析任务 ============
        print("\n" + "=" * 70)
        print(f"  6. 查询分析任务 (req_id={test_req_id})")
        print("=" * 70)
        try:
            task_rows = (await session.execute(text(
                "SELECT id, task_type, status, total_cases, progress, error_msg, created_at, updated_at "
                "FROM analysis_tasks "
                "WHERE req_id = :rid "
                "ORDER BY created_at DESC LIMIT 5"
            ), {"rid": test_req_id})).all()
            if not task_rows:
                print("  （无分析任务记录）")
            for t in task_rows:
                print(f"  - [{t.id}] type={t.task_type} status={t.status} progress={t.progress} cases={t.total_cases}")
                if t.error_msg:
                    print(f"    error: {t.error_msg[:200]}")
        except Exception as e:
            print(f"  ⚠️  任务查询失败: {e}")

        print("\n" + "=" * 70)
        print("  数据库状态查询完成")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
