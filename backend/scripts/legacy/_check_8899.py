"""检查 REQ-1786071385229-8899 的SRR、澄清状态，并探测向量库中是否有相关历史归档向量"""
import sys, asyncio, json
sys.path.insert(0, '/app')
from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select

REQ_ID = "REQ-1786071385229-8899"
SHORT = "8899"

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one_or_none()
        if not r:
            # 试试只匹配后缀8899
            r = (await session.execute(select(Requirement).where(Requirement.id.like('%8899')))).scalars().first()
        if not r:
            # 按标题搜索
            r = (await session.execute(select(Requirement).where(Requirement.title.ilike('%8899%')))).scalars().first()
        if not r:
            print(f"❌ 找不到需求 id包含{SHORT}的记录，列出最近5条需求id：")
            recs = (await session.execute(select(Requirement.id, Requirement.title).order_by(Requirement.created_at.desc()).limit(5))).all()
            for row in recs:
                print(f"  {row[0]}  {row[1][:60]}")
            return
        print("=" * 70)
        print(f"需求ID: {r.id}")
        print(f"  标题: {r.title}")
        print(f"  状态: {r.status}  优先级: {r.priority}")
        print(f"  归档: status={r.status}  archived_at={r.archived_at}")
        print(f"  向量化状态: vectorization_status={r.vectorization_status or '未设置'}")
        print(f"  向量化错误: {r.vectorization_error or '(无)'}")

        srr = r.srr_json or {}
        prd = srr.get("prd", {})
        print()
        print("=" * 70)
        print("【PRD 关键信息扫描】寻找: OPT-A-02版本数量矛盾 / OPT-A-01上传规则引用未定义 / 5%改动比例判定标准")
        # 直接全文搜索关键词
        srr_str = json.dumps(srr, ensure_ascii=False)
        keywords = ["50个", "不设上限", "版本保留", "版本控制", "上传规则", "沿用现有", "5%", "改动比例", "小修订", "正文改动"]
        for kw in keywords:
            count = srr_str.count(kw)
            if count > 0:
                print(f"  ✅ 找到关键词「{kw}」: 出现 {count} 次")
            else:
                print(f"  ⚠️  未找到关键词「{kw}」")

        # 详细打印包含矛盾的字段
        print()
        print("=" * 70)
        print("【详细扫描: 版本保留数量】")
        import re
        # 搜索所有出现数字限制的地方
        version_limits = re.findall(r"[^。；\n]{0,40}(?:版本|历史|保留)[^。；\n]{0,40}(?:[0-9]{1,4}个|不设上限|无上限|无限)[^。；\n]{0,40}", srr_str)
        if version_limits:
            for i, m in enumerate(version_limits, 1):
                print(f"  [{i}] {m[:200]}")
        else:
            print("  ⚠️  未找到版本保留数量相关描述")

        print()
        print("=" * 70)
        print("【详细扫描: 上传规则引用未定义】")
        upload_refs = re.findall(r"[^。；\n]{0,40}(?:沿用|参考|遵循|依照|使用|默认)[^。；\n]{0,40}(?:上传规则|图片规则|附件规则|现有规则)[^。；\n]{0,60}", srr_str)
        if upload_refs:
            for i, m in enumerate(upload_refs, 1):
                print(f"  [{i}] {m[:200]}")
        else:
            print("  ⚠️  未找到上传规则引用")

        print()
        print("=" * 70)
        print("【详细扫描: 5%/改动比例判定标准】")
        ratio_pat = re.findall(r"[^。；\n]{0,30}(?:改动比例|小修订|正文改动|小于5%|< 5%|少于5%|低于5%)[^。；\n]{0,80}", srr_str)
        if ratio_pat:
            for i, m in enumerate(ratio_pat, 1):
                print(f"  [{i}] {m[:200]}")
        else:
            print("  ⚠️  未找到5%改动比例相关描述")

        # 查看澄清状态
        print()
        print("=" * 70)
        cs = srr.get("_clarify_state", {})
        print(f"【澄清状态】本轮已澄清问题数: {len(cs.get('answers', {}))}")
        for qid, ans in list((cs.get("answers") or {}).items())[:15]:
            print(f"  - Q[{qid[:20]}...]: {str(ans)[:60]}")
        existing_qs = cs.get("questions", []) or []
        print(f"【澄清状态】当前问题数: {len(existing_qs)}")
        for q in existing_qs:
            qtext = str(q.get("question", ""))[:80]
            field = q.get("field", "")
            cat = q.get("category", "")
            pri = q.get("priority", "")
            print(f"  - {pri}/{cat} [{field[:40]}]: {qtext}")
    finally:
        await session.close()

asyncio.run(main())
