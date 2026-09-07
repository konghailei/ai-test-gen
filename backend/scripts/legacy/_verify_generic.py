"""通用性验证：对两个不同需求（8899 + 5197 + 再找一个正常需求）分别跑_detect_logical_contradictions，
验证是通用框架，不会过度检查，也不会只对8899有效。"""
import sys, asyncio, json, logging
sys.path.insert(0, '/app')

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
logging.getLogger("app.agents.clarify_agent").setLevel(logging.INFO)

from app.db.database import async_session
from app.db.models import Requirement
from app.agents.clarify_agent import ClarifyAgent
from sqlalchemy import select

TEST_REQS = [
    "REQ-1786071385229-8899",   # 本次优化的核心需求（已知有矛盾+引用未定义）
    "REQ-1786061938371-5197",   # 另一个需求
]

async def pick_another_healthy(session):
    """数据库里取一个不是8899/5197的最新普通需求作为「正常需求」验证不过度检查"""
    r = (await session.execute(
        select(Requirement)
        .where(Requirement.id.notin_(TEST_REQS))
        .where(Requirement.raw_text.is_not(None))
        .order_by(Requirement.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    return r.id if r else None

async def one_case(agent, session, req_id, label):
    r = (await session.execute(select(Requirement).where(Requirement.id == req_id))).scalar_one_or_none()
    if not r:
        print(f"  ❌ 需求不存在: {req_id}")
        return
    srr = r.srr_json if isinstance(r.srr_json, dict) else json.loads(r.srr_json)
    srr["_raw_text"] = r.raw_text or ""
    srr.setdefault("requirement", {})["id"] = r.id
    srr.setdefault("requirement", {})["title"] = r.title or req_id
    t0 = asyncio.get_event_loop().time()
    qs = agent._detect_logical_contradictions(srr)
    dt = asyncio.get_event_loop().time() - t0
    print(f"\n{'='*90}")
    print(f"🧪 [{label}] {req_id} — {r.title or ''}")
    print(f"   candidate条目解析耗时: {dt*1000:.0f}ms   产出questions: {len(qs)} 条")
    print(f"{'='*90}")
    if not qs:
        print(f"   ✅ 无矛盾/引用未定义问题（正常文档，零误报预期）")
        return
    for i, q in enumerate(qs, 1):
        qid = q.get("id","")
        cat = q.get("category","")
        prio = q.get("priority","")
        txt = str(q.get("question","")).replace("\n"," ")[:220]
        print(f"   {i:>2}. {qid} [{prio}/{cat}]: {txt}")
        print(f"       context前120字: {str(q.get('context',''))[:120]}")

async def main():
    async with async_session() as session:
        agent = ClarifyAgent()

        # 先找一个「健康」需求
        healthy = await pick_another_healthy(session)
        print(f"随机抽取的健康需求id: {healthy}")

        cases = [
            (TEST_REQS[0], "目标需求（已知含矛盾+引用未定义，应命中2条左右）"),
            (TEST_REQS[1], "不同需求5197（完全不同主题，验证通用性）"),
        ]
        if healthy:
            cases.append((healthy, "随机普通需求（健康文档，应零条或极少条，验证误报率）"))

        for rid, label in cases:
            await one_case(agent, session, rid, label)

        print("\n\n" + "="*90)
        print("📌 结论：如果 8899 命中 2条（目标2个核心问题命中）、")
        print("   5197 / 健康需求 命中 0-2 条之间（主题不同则结果不同），")
        print("   就证明机制是【通用框架】，而非对8899单独定制！")
        print("="*90)

asyncio.run(main())
