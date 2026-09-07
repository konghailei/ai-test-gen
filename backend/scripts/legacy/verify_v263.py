"""验证 v2.8.63 增强 prompt 后的引用未定义检测能力。

从文件读取真实需求文档，直接调用 ClarifyAgent._generate_questions 方法，
验证 LLM 是否能通过增强后的 prompt（非正则预扫描）识别 OPT-A-01 引用未定义问题。

验证目标：
1. OPT-A-02 跨节矛盾（手动版本保留数量 50 vs 不设上限）→ 应生成 conflict 问题
2. OPT-A-01 引用未定义（图片上传规则未定义具体内容）→ 应生成 ambiguity 问题
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app")

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen",
)

from app.api.settings import load_db_config_to_settings
from app.db.database import async_session
from app.agents.clarify_agent import clarify_agent


async def main():
    async with async_session() as s:
        await load_db_config_to_settings(s)
        print("LLM 配置已加载")

        # 读取真实需求文档
        with open("/app/test_req_doc.md", "r", encoding="utf-8") as f:
            raw_text = f.read()
        print(f"需求文档长度: {len(raw_text)} 字符")

        # 构造最小化 SRR（只含 raw_text 和基本字段）
        srr = {
            "_raw_text": raw_text,
            "requirement": {
                "id": "VERIFY-3535",
                "title": "DevBlog 博客创作平台 v1.1 迭代需求文档",
                "summary": "本次迭代以作者创作效率与体验为核心，系统性优化编辑器能力；同时落地最小内容安全方案。",
            },
            "functional_points": [],
            "flows": {"happy_path": [], "alternate_flows": []},
            "cross_fp_interactions": {"shared_business_rules": []},
        }

        print("\n开始调用 _generate_questions ...")
        result = await clarify_agent._generate_questions(
            srr=srr,
            completeness={"critical": [], "important": []},
            ambiguities=[],
            similar_reqs=[],
            augmentations=[],
        )

        questions = result.get("questions", []) or []
        issues = result.get("issues_detected", []) or []
        print(f"\n生成问题数量: {len(questions)}")
        print(f"检测到 issues 数量: {len(issues)}")
        print("=" * 70)

        conflicts = [q for q in questions if isinstance(q, dict) and q.get("category") == "conflict"]
        ambiguities = [q for q in questions if isinstance(q, dict) and q.get("category") == "ambiguity"]
        others = [q for q in questions if isinstance(q, dict) and q.get("category") not in ("conflict", "ambiguity")]

        print(f"\n【conflict 矛盾问题】({len(conflicts)} 个):")
        for q in conflicts:
            print(f"  - id={q.get('id')}")
            print(f"    field={q.get('field')}")
            print(f"    question={q.get('question')}")
            print(f"    context={str(q.get('context',''))[:200]}")
            print(f"    options={q.get('options', [])}")

        print(f"\n【ambiguity 歧义/引用未定义问题】({len(ambiguities)} 个):")
        for q in ambiguities:
            print(f"  - id={q.get('id')}")
            print(f"    field={q.get('field')}")
            print(f"    question={q.get('question')}")
            print(f"    context={str(q.get('context',''))[:200]}")

        print(f"\n【其他问题】({len(others)} 个):")
        for q in others:
            print(f"  - [{q.get('category')}] {q.get('question','')[:80]}")

        # 也打印 issues_detected
        if issues:
            print(f"\n【issues_detected 详情】({len(issues)} 个):")
            for i, iss in enumerate(issues, 1):
                print(f"  [{i}] type={iss.get('type')} severity={iss.get('severity')} field={iss.get('field')}")
                print(f"      desc={iss.get('description', '')[:300]}")

        print("\n" + "=" * 70)
        print("验证结论:")
        has_opt_a02 = any("50" in (q.get("question", "") + str(q.get("context", ""))) for q in conflicts)
        has_opt_a01 = any(
            "图片" in (q.get("question", "") + str(q.get("context", "")))
            for q in ambiguities
        )
        print(f"  OPT-A-02 跨节矛盾检测: {'✓ 已识别' if has_opt_a02 else '✗ 未识别'}")
        print(f"  OPT-A-01 引用未定义检测: {'✓ 已识别' if has_opt_a01 else '✗ 未识别'}")

        qr = result.get("quality_report", {}) or {}
        print(f"\n质量评分: overall={qr.get('overall_score')} completeness={qr.get('completeness_score')} ambiguity={qr.get('ambiguity_score')}")


if __name__ == "__main__":
    asyncio.run(main())
