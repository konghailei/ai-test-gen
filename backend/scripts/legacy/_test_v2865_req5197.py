# -*- coding: utf-8 -*-
"""在容器内运行：对 REQ-1786061938371-5197 重新调用 ClarifyAgent，验证 v2.8.65 分流逻辑。"""
import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logging.getLogger("app.agents.clarify_agent").setLevel(logging.DEBUG)

async def main():
    from sqlalchemy import select
    from app.db.database import async_session
    from app.db.models import Requirement
    from app.agents.clarify_agent import clarify_agent
    from app.api.settings import load_db_config_to_settings

    # 从数据库加载 LLM 配置到 settings
    async with async_session() as session:
        await load_db_config_to_settings(session)
        print("LLM 配置加载完成")

    req_id = "REQ-1786061938371-5197"
    async with async_session() as session:
        r = await session.execute(select(Requirement).where(Requirement.id == req_id))
        record = r.scalar_one_or_none()
        if not record:
            print(f"需求 {req_id} 不存在")
            return
        print(f"需求: {record.title} | status={record.status}")
        srr = record.srr_json
        srr["_raw_text"] = record.raw_text
        srr["requirement"]["id"] = record.id
        srr["requirement"]["product_id"] = record.product_id
        srr["requirement"]["modules"] = record.modules if isinstance(record.modules, list) else [record.module]

    print("=" * 60)
    print("开始调用 ClarifyAgent.clarify ...")
    result = await clarify_agent.clarify(srr)
    print("=" * 60)
    questions = result.get("questions", [])
    print(f"\n澄清问题总数: {len(questions)}")
    llm_count = sum(1 for q in questions if str(q.get("id", "")).startswith("llm_"))
    kb_count = sum(1 for q in questions if str(q.get("id", "")).startswith("kb_"))
    print(f"  llm_ 前缀（直接人工 conflict）: {llm_count}")
    print(f"  kb_  前缀（经 KB 查询）: {kb_count}")
    print("\n问题列表:")
    for q in questions:
        print(f"  [{q.get('id','?'):<28s}] cat={q.get('category','?'):<14s} field={q.get('field','?')[:40]}")
        print(f"    -> {q.get('question','')[:80]}")

    kb_logs = result.get("kb_access_log", [])
    print(f"\nKB 查询日志: {len(kb_logs)} 条")
    for l in kb_logs:
        print(f"  [{l.get('type','?'):<20s}] ans={l.get('answered','?'):<5} cnt={l.get('result_count','?')}  {str(l.get('query',''))[:60]}")
    print(f"\nKB 统计: answered={result.get('kb_answered_count',0)}, total={result.get('kb_total_count',0)}")

asyncio.run(main())
