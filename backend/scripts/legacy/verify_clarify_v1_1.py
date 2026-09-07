"""v1.1 需求文档澄清问题识别完整测试。

对两份需求文档（v1.1优化需求 + v1.0 PRD）分别运行完整的 ClarifyAgent.clarify()
流程，验证澄清问题识别的准确性，包括：
1. OPT-A-02 跨节矛盾检测（手动版 50 vs 不设上限）
2. OPT-A-01 引用未定义检测（图片上传规则引用）
3. PRD v1.0 中的内容安全缺失与风险提示的一致性问题
4. 其他逻辑矛盾、模糊表述、边界条件缺失

运行方式:
  docker compose exec backend python /app/verify_clarify_v1_1.py
"""
import asyncio
import os
import sys
import json

sys.path.insert(0, "/app")

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen",
)

from app.api.settings import load_db_config_to_settings
from app.db.database import async_session
from app.agents.clarify_agent import clarify_agent


# 预期应该被检测到的关键问题（用于验证准确性）
EXPECTED_ISSUES_V11 = [
    {
        "name": "OPT-A-02 手动版本数量矛盾（50 vs 不设上限）",
        "keywords": ["50", "上限", "手动", "标记", "矛盾", "conflict"],
        "type": "conflict",
    },
    {
        "name": "OPT-A-01 图片上传规则引用未定义",
        "keywords": ["图片", "上传规则", "现有", "沿用", "引用", "未定义"],
        "type": "ambiguity",
    },
    {
        "name": "小修订改动比例 5% 判定标准未定义",
        "keywords": ["5%", "小修订", "改动", "正文", "比例"],
        "type": "ambiguity",
    },
    {
        "name": "敏感词拦截提示不告知具体命中词的合规边界",
        "keywords": ["不告知", "命中词", "拦截", "提示", "敏感词"],
        "type": "ambiguity",
    },
    {
        "name": "内容安全与范围外声明的矛盾（PRD说不做但v1.1说要做）",
        "keywords": ["管理后台", "内容审核", "不做", "范围外", "最小方案"],
        "type": "conflict",
    },
]

EXPECTED_ISSUES_PRD = [
    {
        "name": "范围外声明与风险评估的矛盾（说不做管理后台又建议最小方案）",
        "keywords": ["管理后台", "内容审核", "范围外", "最小方案", "建议"],
        "type": "conflict",
    },
    {
        "name": "历史版本回滚数量定义（20版）缺少清理规则",
        "keywords": ["20", "历史版本", "回滚", "保留", "清理"],
        "type": "ambiguity",
    },
    {
        "name": "第三方登录微信企业资质降级方案未明确触发条件",
        "keywords": ["微信", "企业资质", "降级", "GitHub", "Google"],
        "type": "ambiguity",
    },
    {
        "name": "权限模型缺少管理员角色但风险评估又需要运营处理能力",
        "keywords": ["管理员", "角色", "运营", "权限", "处理"],
        "type": "ambiguity",
    },
]


def build_srr(raw_text: str, req_id: str, title: str, summary: str, product_id: str = "PROD-DEVBLG") -> dict:
    """构造最小化 SRR。"""
    return {
        "_raw_text": raw_text,
        "requirement": {
            "id": req_id,
            "title": title,
            "summary": summary,
            "product_id": product_id,
        },
        "functional_points": [],
        "flows": {"happy_path": [], "alternate_flows": []},
        "cross_fp_interactions": {"shared_business_rules": []},
    }


def check_issue_detected(issue_def: dict, questions: list, issues: list) -> tuple[bool, list[str]]:
    """检查预期问题是否被检测到。

    Returns: (是否检测到, 匹配证据列表)
    """
    evidences = []
    keywords = issue_def["keywords"]
    target_type = issue_def.get("type", "")

    # 从 questions 中匹配
    for q in questions:
        if not isinstance(q, dict):
            continue
        text = (
            str(q.get("question", ""))
            + " " + str(q.get("context", ""))
            + " " + str(q.get("field", ""))
            + " " + str(q.get("category", ""))
        )
        cat = q.get("category", "")
        type_match = (not target_type) or (cat == target_type) or (cat and target_type in cat)
        kw_match = all(any(kw.lower() in text.lower() for kw in [k]) for k in keywords[:2])
        kw_count = sum(1 for k in keywords if k.lower() in text.lower())
        if kw_count >= 2 and (type_match or kw_count >= 3):
            evidences.append(f"Q[{q.get('id','?')}][{cat}]: {str(q.get('question',''))[:120]}")

    # 从 issues 中匹配
    for iss in issues:
        if not isinstance(iss, dict):
            continue
        text = (
            str(iss.get("description", ""))
            + " " + str(iss.get("field", ""))
            + " " + str(iss.get("type", ""))
        )
        itype = iss.get("type", "")
        type_match = (not target_type) or (itype == target_type)
        kw_count = sum(1 for k in keywords if k.lower() in text.lower())
        if kw_count >= 2 and (type_match or kw_count >= 3):
            evidences.append(f"Issue[{itype}][{iss.get('severity','?')}]: {str(iss.get('description',''))[:120]}")

    return (len(evidences) > 0, evidences)


async def test_one_doc(doc_path: str, req_id: str, title: str, summary: str, expected_issues: list, label: str):
    """测试单份文档。"""
    print(f"\n{'='*80}")
    print(f"📄 {label}: {title}")
    print(f"📁 文件: {doc_path}")
    print(f"{'='*80}")

    with open(doc_path, "r", encoding="utf-8") as f:
        raw_text = f.read()
    print(f"文档长度: {len(raw_text)} 字符")

    srr = build_srr(raw_text, req_id, title, summary)
    print(f"构造 SRR 完成，开始澄清分析...\n")

    # 进度回调：打印进度
    async def progress_cb(msg: str, pct: int):
        print(f"  进度 {pct}%: {msg}")

    t0 = asyncio.get_event_loop().time()
    result = await clarify_agent.clarify(srr, progress_callback=progress_cb)
    elapsed = asyncio.get_event_loop().time() - t0
    print(f"\n✅ 澄清完成，耗时: {elapsed:.1f} 秒")

    status = result.get("status", "?")
    qr = result.get("quality_report", {}) or {}
    kbs = result.get("kb_status", {}) or {}
    print(f"  状态: {status}")
    print(f"  质量评分: overall={qr.get('overall_score')}  completeness={qr.get('completeness_score')}  ambiguity={qr.get('ambiguity_score')}")
    print(f"  KB 状态: kb_empty={kbs.get('kb_empty')}  product_first={kbs.get('product_first_requirement')}")

    questions = result.get("questions", []) or []
    issues = result.get("issues_detected", []) or []
    augs = result.get("auto_augmentations", []) or []
    print(f"  澄清问题数: {len(questions)}  |  issues_detected: {len(issues)}  |  KB 补全字段: {len(augs)}")

    # 按类别分组
    by_cat: dict[str, list] = {}
    for q in questions:
        if isinstance(q, dict):
            cat = q.get("category", q.get("priority", "misc"))
            by_cat.setdefault(cat, []).append(q)

    print(f"\n📋 问题分类统计:")
    for cat, qs in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        print(f"  - {cat}: {len(qs)} 个")

    # 打印每个问题详情
    print(f"\n📝 所有澄清问题详情:")
    for i, q in enumerate(questions, 1):
        if not isinstance(q, dict):
            continue
        pid = q.get("id", f"?{i}")
        pri = q.get("priority", "?")
        cat = q.get("category", "?")
        field = q.get("field", "")
        quest = q.get("question", "")
        ctx = str(q.get("context", ""))[:150]
        print(f"\n  [{i}] Q{pid} [{pri}|{cat}] field={field}")
        print(f"      Q: {quest}")
        if ctx:
            print(f"      Ctx: {ctx}")
        opts = q.get("options", [])
        if opts:
            print(f"      Options: {opts[:5]}{'...' if len(opts)>5 else ''}")

    # 验证预期问题
    print(f"\n🔍 预期问题验证:")
    passed = 0
    failed = 0
    for issue_def in expected_issues:
        detected, evidences = check_issue_detected(issue_def, questions, issues)
        if detected:
            passed += 1
            print(f"  ✅ {issue_def['name']}")
            for ev in evidences[:2]:
                print(f"     ↳ 证据: {ev}")
        else:
            failed += 1
            print(f"  ❌ {issue_def['name']}  (未检测到)")
            print(f"     ↳ 期望关键词: {issue_def['keywords']} 类型: {issue_def.get('type','any')}")

    print(f"\n📊 验证结果: {passed}/{len(expected_issues)} 通过, {failed} 未通过")
    return {
        "label": label,
        "doc_path": doc_path,
        "elapsed_sec": round(elapsed, 1),
        "status": status,
        "quality": qr,
        "questions_count": len(questions),
        "issues_count": len(issues),
        "expected_passed": passed,
        "expected_total": len(expected_issues),
        "expected_failed": failed,
        "questions": questions,
        "issues_detected": issues,
    }


async def main():
    async with async_session() as s:
        await load_db_config_to_settings(s)
    print("✅ DB 配置 & LLM 配置已加载\n")

    results = []

    # ===== 文档 1: v1.1 优化需求文档 =====
    v11_paths = [
        "/app/tests/博客创作平台-v1.1优化需求文档.md",
        "/app/test_req_doc.md",
    ]
    v11_path = None
    for p in v11_paths:
        if os.path.exists(p):
            v11_path = p
            break

    if v11_path:
        r1 = await test_one_doc(
            doc_path=v11_path,
            req_id="VERIFY-V11-OPT",
            title="DevBlog 博客创作平台 v1.1 迭代需求文档",
            summary="本次迭代以作者创作效率与体验为核心，系统性优化编辑器能力（图片素材库、版本对比、命令菜单等）；同时落地最小内容安全方案（敏感词过滤、举报处置、新账号风控）。",
            expected_issues=EXPECTED_ISSUES_V11,
            label="文档1: v1.1 优化需求",
        )
        results.append(r1)
    else:
        print(f"\n⚠️  未找到 v1.1 优化需求文档（已尝试: {v11_paths}），跳过")

    # ===== 文档 2: v1.0 PRD 产品需求文档 =====
    # 需要先把 PRD 文档复制到容器可访问路径，这里尝试多种路径
    prd_paths = [
        "/app/tests/博客创作平台-产品需求文档PRD.md",
        "/app/博客创作平台-产品需求文档PRD.md",
        "/app/PRD.md",
    ]
    prd_path = None
    for p in prd_paths:
        if os.path.exists(p):
            prd_path = p
            break

    if prd_path:
        r2 = await test_one_doc(
            doc_path=prd_path,
            req_id="VERIFY-V10-PRD",
            title="DevBlog 技术博客创作平台 PRD v1.0",
            summary="DevBlog 是面向技术开发者的多用户博客创作平台，覆盖账户体系、内容创作与管理、社区互动、搜索与通知、数据统计等全部功能域。",
            expected_issues=EXPECTED_ISSUES_PRD,
            label="文档2: v1.0 产品PRD",
        )
        results.append(r2)
    else:
        print(f"\n⚠️  未找到 PRD 文档（已尝试: {prd_paths}），跳过 PRD 测试")
        print("提示: 需要先把 PRD 文档复制到容器内的 /app/ 目录下")

    # ===== 汇总 =====
    print(f"\n\n{'#'*80}")
    print(f"# 📊 总体测试结果汇总")
    print(f"{'#'*80}")
    total_pass = sum(r["expected_passed"] for r in results)
    total_total = sum(r["expected_total"] for r in results)
    total_q = sum(r["questions_count"] for r in results)
    print(f"\n  测试文档数: {len(results)}")
    print(f"  总澄清问题数: {total_q}")
    print(f"  预期问题验证: {total_pass}/{total_total} 通过 ({(total_pass/total_total*100):.0f}%)" if total_total else "  无预期验证项")
    print(f"\n  各文档明细:")
    for r in results:
        qscore = r['quality'].get('overall_score', '?')
        rate = f"{r['expected_passed']}/{r['expected_total']}" if r['expected_total'] else "N/A"
        print(f"    - {r['label']}: 状态={r['status']} 质量分={qscore} 问题={r['questions_count']} 验证={rate} 耗时={r['elapsed_sec']}s")

    # 保存完整 JSON 结果
    out_path = "/app/clarify_v1_1_results.json"
    try:
        # 去掉 questions 全文（太长），只保留摘要
        slim = []
        for r in results:
            s = {k: v for k, v in r.items() if k not in ("questions", "issues_detected")}
            s["questions_summary"] = [
                {
                    "id": q.get("id"), "priority": q.get("priority"),
                    "category": q.get("category"), "field": q.get("field"),
                    "question": str(q.get("question", ""))[:200],
                }
                for q in (r["questions"] if isinstance(r.get("questions"), list) else [])
                if isinstance(q, dict)
            ]
            slim.append(s)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        print(f"\n  💾 完整结果已保存: {out_path}")
    except Exception as e:
        print(f"\n  ⚠️  保存结果失败: {e}")

    print(f"\n{'='*80}")
    print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
