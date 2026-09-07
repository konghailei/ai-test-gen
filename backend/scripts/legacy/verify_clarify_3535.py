"""直接调用 ClarifyAgent 对 REQ-1785997647705-3535 进行分析，验证 v2.8.63 增强 prompt 后的效果。

验证目标：
1. OPT-A-02 跨节矛盾（手动版本保留数量 50 vs 不设上限）→ 应生成 conflict 问题
2. OPT-A-01 引用未定义（图片上传规则未定义具体内容）→ 应生成 ambiguity 问题（通过增强 prompt，而非正则预扫描）
"""
import asyncio
import os
import sys
import copy

sys.path.insert(0, "/app")

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen",
)

from sqlalchemy import select
from app.db.database import async_session
from app.db.models import Requirement
from app.agents.clarify_agent import clarify_agent
from app.api.settings import load_db_config_to_settings


async def main():
    async with async_session() as s:
        await load_db_config_to_settings(s)
        print("LLM 配置已加载到 settings 单例")

        req = await s.execute(
            select(Requirement).where(Requirement.id == "REQ-1785997647705-3535")
        )
        r = req.scalar_one_or_none()
        if not r:
            print("需求不存在")
            return

        print(f"需求: {r.id} | {r.title} | status={r.status}")
        print(f"raw_text 长度: {len(r.raw_text or '')}")

        srr = copy.deepcopy(r.srr_json or {})
        srr["_raw_text"] = r.raw_text or ""
        srr.pop("_clarify_state", None)
        srr.pop("clarify_answers", None)

        print("\n开始调用 ClarifyAgent.clarify() ...")
        result = await clarify_agent.clarify(srr)

        questions = result.get("questions", []) or []
        print(f"\n生成问题数量: {len(questions)}")
        print("=" * 70)

        conflicts = []
        ambiguities = []
        others = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            cat = q.get("category", "")
            if cat == "conflict":
                conflicts.append(q)
            elif cat == "ambiguity":
                ambiguities.append(q)
            else:
                others.append(q)

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
        print(f"\n质量评分: overall={qr.get('overall_score')}")
        print(f"状态: {result.get('status')}")


if __name__ == "__main__":
    asyncio.run(main())
