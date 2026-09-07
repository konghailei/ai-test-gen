"""最小化跑完整clarify链路，看3个关键问题是否在最终输出"""
import sys, asyncio, json, logging, time
sys.path.insert(0, '/app')

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s:%(lineno)d | %(message)s")
logging.getLogger("app.agents.clarify_agent").setLevel(logging.WARNING)

from app.db.database import get_session
from app.db.models import Requirement
from sqlalchemy import select
from app.agents.clarify_agent import ClarifyAgent

REQ_ID = "REQ-1786071385229-8899"

async def main():
    session_iter = get_session()
    session = await session_iter.__anext__()
    try:
        r = (await session.execute(select(Requirement).where(Requirement.id == REQ_ID))).scalar_one()
        srr = r.srr_json or {}
        product_id = srr.get("requirement", {}).get("product_id", r.product_id) or ""
        project_id = srr.get("requirement", {}).get("project_id", r.project_id) or ""
        print(f"=" * 80)
        print(f"  需求: {r.id}   product_id={product_id}  project_id={project_id}")
        print(f"=" * 80)

        agent = ClarifyAgent()
        async def rep(msg, p):
            pass  # 静默
        t0 = time.time()
        result = await agent.clarify(
            srr=srr,
            progress_callback=rep,
        )
        dt = time.time() - t0
        qs = result.get("questions", [])
        print(f"\n✅ agent.clarify 完成！耗时 {dt:.1f}s，最终生成 {len(qs)} 条澄清问题：")
        print("=" * 80)

        targets = {
            "OPT-A-02_版本数量50vs不设上限": False,
            "OPT-A-01_图片上传规则沿用": False,
            "小修订_5%改动比例": False,
        }
        for i, q in enumerate(qs, 1):
            qtext = str(q.get("question","")).replace("\n", " ")[:220]
            prio = q.get("priority","")
            cat  = q.get("category","")
            qid  = q.get("id","")
            opts = q.get("options") or []
            print(f"\n[{i:2d}] {qid} [{prio}/{cat}] {qtext}")
            if opts:
                print(f"     options({len(opts)}): " + " / ".join(str(o)[:60] for o in opts[:5]))

            # 检查3个关键目标
            if ("50" in qtext and ("不设上限" in qtext or "上限" in qtext)) or ("手动标记" in qtext and "保留" in qtext) or "版本保留数量" in qtext:
                targets["OPT-A-02_版本数量50vs不设上限"] = True
                print("     ⭐⭐⭐ 命中目标1：版本数量矛盾")
            if "沿用" in qtext or ("现有图片上传规则" in qtext) or ("引用未定义" in str(q.get("context","")) and ("图片" in qtext or "素材库" in qtext)):
                targets["OPT-A-01_图片上传规则沿用"] = True
                print("     ⭐⭐⭐ 命中目标2：图片上传规则引用未定义")
            if ("5%" in qtext or "改动比例" in qtext or "小修订" in qtext) and ("字符" in qtext or "段落" in qtext or "编辑距离" in qtext or "单词" in qtext or "计算方式" in qtext):
                targets["小修订_5%改动比例"] = True
                print("     ⭐⭐⭐ 命中目标3：5%改动比例判定标准")

        print("\n" + "=" * 80)
        print("最终命中情况汇总：")
        all_hit = True
        for name, ok in targets.items():
            flag = "✅ 已命中" if ok else "❌ 未命中"
            all_hit = all_hit and ok
            print(f"  {flag} → {name}")
        print(f"\n总评: {'🎉 3项全部命中！' if all_hit else '⚠️ 有缺失项，请继续排查'}")
    finally:
        await session.close()

asyncio.run(main())
