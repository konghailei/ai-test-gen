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
        srr = record.srr_json or {}
        print(f'SRR keys: {list(srr.keys())}')
        for k in ['questions', 'clarify_questions', 'quality_report', 'kb_status', 'legacy_data_impact']:
            v = srr.get(k)
            if v is None: continue
            if isinstance(v, list):
                print(f'\n=== srr.{k} count={len(v)} ===')
                for i, q in enumerate(v[:40]):
                    qid = q.get('id', '?') if isinstance(q, dict) else '?'
                    res = q.get('resolved') if isinstance(q, dict) else '?'
                    cat = q.get('category') if isinstance(q, dict) else '?'
                    qtxt = q.get('question', '') if isinstance(q, dict) else str(q)
                    print(f'  [{i+1:02d}] res={str(res):5s} cat={str(cat):14s} id={str(qid)[:45]}')
                    print(f'       Q: {str(qtxt)[:70]}')
                    ans = q.get('answer') if isinstance(q, dict) else None
                    if ans:
                        print(f'       A: {str(ans)[:50]}...')
            else:
                sval = json.dumps(v, ensure_ascii=False)
                print(f'\n=== srr.{k} ===')
                print(sval[:2000])
asyncio.run(main())
