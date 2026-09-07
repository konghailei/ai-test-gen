# -*- coding: utf-8 -*-
import asyncio, json
from sqlalchemy import select
from app.db.database import async_session
from app.db.models import Requirement

async def main():
    req_id = 'REQ-1786064886558-5455'
    async with async_session() as session:
        r = await session.execute(select(Requirement).where(Requirement.id == req_id))
        record = r.scalar_one_or_none()
        if not record:
            print('需求不存在')
            return
        print(f'需求: {record.title} | status={record.status} | revision={record.revision}')
        cq = record.clarify_questions or []
        print(f'澄清问题总数: {len(cq)}')
        for i, q in enumerate(cq[:50]):
            resolved = q.get('resolved', False)
            ans = (q.get('answer') or '')[:30]
            qid = q.get('id', '?')
            cat = q.get('category', '?')
            qtext = str(q.get('question', ''))[:60]
            print(f'  [{i+1:02d}] res={str(resolved):5s} cat={cat:14s} id={str(qid)[:45]}')
            print(f'       Q: {qtext}')
            if ans:
                print(f'       A: {ans}...')
        srr = record.srr_json or {}
        ldi = srr.get('legacy_data_impact', {})
        print(f'\n=== legacy_data_impact in SRR ===')
        print(json.dumps(ldi, ensure_ascii=False, indent=2)[:1500])
asyncio.run(main())
