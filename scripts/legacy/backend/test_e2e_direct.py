"""端到端测试 - 直接调用 orchestrator 同步执行，避免 HTTP 阻塞。"""
import asyncio
import json
import sys
import time
import os
from datetime import datetime

# 设置 token 环境变量供可能用到
os.environ.setdefault("TESTING", "1")


async def init_settings():
    """从 DB 加载 LLM 配置（模拟后端启动流程）。"""
    from app.db.database import async_session
    from app.api.settings import load_db_config_to_settings
    async with async_session() as session:
        await load_db_config_to_settings(session)
    from app.config import settings
    print(f"✓ LLM 配置已加载: provider={settings.LLM_PROVIDER}")


async def reset_and_get_requirement():
    """重置卡死需求并返回一个可用于生成的需求。"""
    from app.db.database import async_session
    from app.db.models import Requirement, AnalysisTask
    from sqlalchemy import select, update

    async with async_session() as s:
        # 查找卡死的需求
        rs = await s.execute(
            select(Requirement).where(Requirement.status == "analyzing")
        )
        for r in rs.scalars():
            r.status = "draft"
            print(f"重置 {r.id} (analyzing → draft)")
        await s.commit()

        # 取出测试需求
        rs = await s.execute(
            select(Requirement).where(Requirement.id == "REQ-1784473087033-4566")
        )
        req = rs.scalars().first()
        return req


async def parse_requirement(req):
    """直接调用 ReqAgent 解析需求。"""
    from app.agents.req_agent import req_agent
    from app.db.database import async_session
    from app.db.models import Requirement, FunctionalPoint
    from sqlalchemy import select

    print(f"\n[阶段 1] 解析需求: {req.id} - {req.title}")
    srr = await req_agent.parse(
        raw_requirement=req.raw_text,
        product_id=req.product_id or "PROD-DEFAULT",
        product_name="",
    )
    # 捕获 FP（parse 调用后会填充 req_agent.last_fps）
    captured_fps = list(req_agent.last_fps)
    captured_fp_deps = list(req_agent.last_fp_dependencies)

    # 保存到 DB
    async with async_session() as s:
        rs = await s.execute(select(Requirement).where(Requirement.id == req.id))
        r = rs.scalars().first()
        r.srr_json = srr
        r.status = "pending_review"
        req_data = srr.get("requirement", {})
        r.title = req_data.get("title", r.title)
        r.priority = req_data.get("priority", r.priority)
        r.modules = req_data.get("modules", [])

        # 保存功能点
        # 先删除旧的
        existing = await s.execute(
            select(FunctionalPoint).where(FunctionalPoint.requirement_id == req.id)
        )
        for fp in existing.scalars():
            await s.delete(fp)
        # 插入新的
        for fp_dict in captured_fps:
            if isinstance(fp_dict, dict):
                fp = FunctionalPoint(
                    id=fp_dict.get("id") or f"FP-{int(time.time()*1000000)}",
                    requirement_id=req.id,
                    name=fp_dict.get("name", ""),
                    fp_type=fp_dict.get("fp_type", "core"),
                    module_id=fp_dict.get("module_id"),
                    srr_json=fp_dict.get("srr_json", {}),
                    test_hints=fp_dict.get("test_hints", []),
                    status="approved",  # 直接审核通过
                )
                s.add(fp)
        await s.commit()

    print(f"  ✓ 解析完成: status=pending_review")
    req_obj = srr.get("requirement", {})
    print(f"    标题: {req_obj.get('title')}")
    print(f"    优先级: {req_obj.get('priority')}")
    print(f"    模块: {req_obj.get('modules', [])}")
    print(f"    功能点数: {len(captured_fps)}")
    for fp in captured_fps[:8]:
        if isinstance(fp, dict):
            print(f"      - [{fp.get('fp_type','?')}] {fp.get('name','?')}")
    qr = srr.get("quality_report", {})
    print(f"    质量评分: overall={qr.get('overall_score')} completeness={qr.get('completeness_score')}")
    return srr


async def approve_functional_points(req_id, srr):
    """审核 SRR 中的功能点（直接写 DB）。"""
    from app.db.database import async_session
    from app.db.models import FunctionalPoint
    from sqlalchemy import select, update

    print(f"\n[阶段 2] 审核功能点")
    async with async_session() as s:
        rs = await s.execute(
            select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
        )
        fps = rs.scalars().all()
        print(f"  数据库功能点数: {len(fps)}")
        for fp in fps:
            if fp.status != "approved":
                fp.status = "approved"
        await s.commit()
        print(f"  ✓ 全部审核完成 ({len(fps)} 个)")


async def confirm_requirement(req_id):
    """将需求状态置为 confirmed。"""
    from app.db.database import async_session
    from app.db.models import Requirement
    from sqlalchemy import select

    print(f"\n[阶段 3] 确认需求")
    async with async_session() as s:
        rs = await s.execute(select(Requirement).where(Requirement.id == req_id))
        r = rs.scalars().first()
        r.status = "confirmed"
        await s.commit()
    print(f"  ✓ 状态: confirmed")


async def run_generation(req, srr):
    """直接调用 orchestrator.run_pipeline 生成测试用例。"""
    from app.core.orchestrator import orchestrator

    print(f"\n[阶段 4] 生成测试用例（直接调用 orchestrator）")
    # 调试：打印 srr 概要
    print(f"  [调试] srr type={type(srr).__name__}, keys={list(srr.keys()) if isinstance(srr, dict) else 'N/A'}")
    if isinstance(srr, dict):
        req_obj = srr.get("requirement", {})
        print(f"  [调试] srr.requirement.id={req_obj.get('id')}, title={req_obj.get('title','')[:30]}")

    cases = []
    review_report = None
    kb_flowback = None
    gen_error = None
    start = time.time()

    try:
        async for evt in orchestrator.run_pipeline(
            raw_requirement=req.raw_text,
            srr_json=srr,
            product_id=req.product_id or "PROD-DEFAULT",
            product_name="",
            clarify_answers={},
            existing_req_id=req.id,
        ):
            stage = evt.get("stage", "")
            msg = evt.get("message", "")
            progress = evt.get("progress", 0)
            print(f"  [{stage}] {progress}% {msg[:60]}")

            if stage == "done":
                cases = evt.get("cases", [])
                review_report = evt.get("review_report")
                kb_flowback = evt.get("kb_flowback")
                break
            if stage == "error":
                gen_error = msg
                break
    except Exception as e:
        import traceback
        gen_error = str(e)
        traceback.print_exc()

    elapsed = time.time() - start
    print(f"\n  生成耗时: {elapsed:.1f}s")

    if gen_error:
        print(f"  ✗ 生成失败: {gen_error}")
        return [], None, None

    print(f"  ✓ 生成完成: 共 {len(cases)} 个用例")
    return cases, review_report, kb_flowback


def analyze_quality(cases, review_report, kb_flowback):
    """分析生成质量。"""
    print(f"\n[阶段 5] 生成结果质量分析")
    if not cases:
        print("  ✗ 无用例")
        return

    type_count = {}
    priority_count = {}
    step_counts = []
    conf_vals = []
    missing_fields = []

    for c in cases:
        t = c.get("type", "unknown")
        type_count[t] = type_count.get(t, 0) + 1
        p = c.get("priority", "?")
        priority_count[p] = priority_count.get(p, 0) + 1
        steps = c.get("steps", [])
        step_counts.append(len(steps))
        gm = c.get("gen_metadata", {})
        conf = gm.get("gen_confidence") or c.get("confidence", 0)
        conf_vals.append(conf)
        for field in ("id", "title", "type", "steps", "assertions"):
            if not c.get(field):
                missing_fields.append(f"{c.get('id','?')}.{field}")

    print(f"  类型分布: {type_count}")
    print(f"  优先级分布: {priority_count}")
    if step_counts:
        print(f"  步骤数: 平均 {sum(step_counts)/len(step_counts):.1f}, 范围 [{min(step_counts)}, {max(step_counts)}]")
    if conf_vals:
        print(f"  置信度: 平均 {sum(conf_vals)/len(conf_vals):.2f}, 范围 [{min(conf_vals):.2f}, {max(conf_vals):.2f}]")
    if missing_fields:
        print(f"  ✗ 缺失字段: {missing_fields[:5]}")
    else:
        print(f"  ✓ 必填字段完整")

    # ID 唯一性
    ids = [c.get("id") for c in cases]
    if len(set(ids)) == len(ids):
        print(f"  ✓ ID 唯一性: 通过 ({len(ids)} 个 ID 全部唯一)")
    else:
        print(f"  ✗ ID 唯一性: 失败！{len(ids) - len(set(ids))} 个重复")

    # 样例
    print(f"\n  用例样例（前 3 个）:")
    for c in cases[:3]:
        print(f"\n    [{c.get('id')}] [{c.get('type')}] {c.get('title')}")
        for i, s in enumerate(c.get("steps", [])[:3]):
            if isinstance(s, dict):
                print(f"      步骤{i+1}: {s.get('action','')} → {s.get('expected','')}")
        if c.get("assertions"):
            for a in c["assertions"][:2]:
                if isinstance(a, dict):
                    print(f"      断言: {a.get('assertion','') or a.get('expected','')}")

    if review_report:
        print(f"\n  审核报告:")
        print(f"    总用例: {review_report.get('total_cases')}")
        print(f"    通过: {review_report.get('passed_cases')}")
        print(f"    Blocker: {review_report.get('blocked_cases')}")
        avg_conf = review_report.get("avg_confidence", 0)
        print(f"    平均置信度: {avg_conf*100:.0f}%")
        issues = review_report.get("issues", [])
        if issues:
            print(f"    问题列表 ({len(issues)} 项):")
            for iss in issues[:5]:
                if isinstance(iss, dict):
                    print(f"      [{iss.get('severity')}] {iss.get('case_id')}: {iss.get('message','')[:60]}")

    if kb_flowback:
        print(f"\n  知识库回流:")
        print(f"    自动入库: {kb_flowback.get('auto_approved', 0)}")
        print(f"    待审核: {kb_flowback.get('pending_review', 0)}")
        print(f"    已驳回: {kb_flowback.get('rejected', 0)}")
        print(f"    入库错误: {kb_flowback.get('errors', 0)}")


async def test_exports(cases, req):
    """测试导出功能（直接调用导出器）。"""
    import json as _json
    from app.exporters import export_markdown, export_gherkin, export_pytest
    from app.exporters.xmind import export_to_xmind

    print(f"\n[阶段 6] 测试导出功能（5 种格式）")
    if not cases:
        print("  ✗ 无用例可导出")
        return

    def export_json_impl(c):
        return _json.dumps(c, ensure_ascii=False, indent=2)

    formats = [
        ("json", lambda c, p: export_json_impl(c)),
        ("markdown", lambda c, p: export_markdown(c)),
        ("gherkin", lambda c, p: export_gherkin(c)),
        ("pytest", lambda c, p: export_pytest(c)),
        ("xmind", lambda c, p: export_to_xmind(c, p)),
    ]
    ext_map = {"json": "json", "markdown": "md", "gherkin": "feature", "pytest": "py", "xmind": "xmind"}
    project_name = "电商订单系统"
    for name, fn in formats:
        try:
            result = fn(cases, project_name)
            if isinstance(result, bytes):
                size = len(result)
            elif isinstance(result, str):
                size = len(result)
            else:
                size = len(str(result))
            print(f"  ✓ {name}: 导出成功 ({size} bytes)")
            out_path = f"/app/test_output_{name}.{ext_map[name]}"
            with open(out_path, "wb") as f:
                if isinstance(result, bytes):
                    f.write(result)
                else:
                    f.write(str(result).encode("utf-8"))
        except Exception as e:
            import traceback
            print(f"  ✗ {name}: 导出失败 - {e}")
            traceback.print_exc()


async def main():
    print("=" * 70)
    print("  AI测试用例生成系统 - 端到端测试（直接调用底层模块）")
    print("=" * 70)

    # 0. 加载 LLM 配置
    await init_settings()

    # 1. 重置并取出测试需求
    req = await reset_and_get_requirement()
    if not req:
        print("✗ 测试需求不存在")
        return False
    print(f"\n测试需求: {req.id} ({req.status}) {req.title}")

    # 2. 解析需求（如已 confirmed 则复用 SRR，避免重复 LLM 调用）
    if req.status == "confirmed" and req.srr_json:
        srr = req.srr_json
        print(f"\n[阶段 1] 需求已解析并确认，复用 SRR (status={req.status})")
        req_obj = srr.get("requirement", {})
        print(f"    标题: {req_obj.get('title')}")
        print(f"    模块: {req_obj.get('modules', [])}")
    elif not req.srr_json or req.status == "draft":
        srr = await parse_requirement(req)
    else:
        srr = req.srr_json
        print(f"\n[阶段 1] 需求已解析，复用 SRR (status={req.status})")

    # 3. 审核功能点（仅当未审核时）
    if req.status != "confirmed":
        await approve_functional_points(req.id, srr)

    # 4. 确认需求（仅当未确认时）
    if req.status != "confirmed":
        await confirm_requirement(req.id)

    # 5. 生成测试用例
    cases, review_report, kb_flowback = await run_generation(req, srr)

    # 6. 质量分析
    analyze_quality(cases, review_report, kb_flowback)

    # 7. 测试导出
    await test_exports(cases, req)

    # 8. 总结
    print(f"\n{'=' * 70}")
    print(f"  端到端测试总结")
    print(f"{'=' * 70}")
    print(f"  需求 ID: {req.id}")
    print(f"  需求标题: {req.title}")
    print(f"  生成用例数: {len(cases)}")
    if review_report:
        print(f"  审核通过: {review_report.get('passed_cases', 0)}/{review_report.get('total_cases', 0)}")
    print(f"\n=== 端到端测试", "通过 ===" if cases else "失败 ===")
    return bool(cases)


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
