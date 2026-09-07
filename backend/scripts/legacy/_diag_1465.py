"""REQ-1465 全面诊断：问题列表/KB日志/优化影响重复/图片上传KB/长问题"""
import asyncio, json
from app.db.database import async_session
from app.db.models import Requirement
from sqlalchemy import select

async def main():
    async with async_session() as s:
        r = (await s.execute(select(Requirement).where(Requirement.id=='REQ-1786086120822-1465'))).scalar_one_or_none()
        if not r:
            print('NOT FOUND'); return
        print('title:', r.title, '| status:', r.status, '| product:', r.product_id)
        srr = r.srr_json if isinstance(r.srr_json,dict) else json.loads(r.srr_json)
        cs = srr.get('_clarify_state') or {}
        qs = cs.get('questions') or []
        print('\n=== 人工澄清问题 (共%d条) ===' % len(qs))
        for i,q in enumerate(qs,1):
            if not isinstance(q,dict): continue
            qid=q.get('id','?'); cat=q.get('category','?'); pri=q.get('priority','?')
            qt=str(q.get('question',''))
            opts=q.get('options',[]) or []
            field=q.get('field','')
            print(f'\n--- {i}. {qid} [{pri}/{cat}] field={field} opts={len(opts)} ---')
            print(f'   Q(len={len(qt)}): {qt}')
            if opts: print(f'   opts: {opts[:5]}')
        # KB logs
        kql = srr.get('_kb_query_logs') or cs.get('kb_access_log') or []
        print('\n\n=== KB 查询日志 (共%d条) ===' % len(kql))
        for i,e in enumerate(kql,1):
            if not isinstance(e,dict): continue
            typ=e.get('type','?'); ans=e.get('answered'); rc=e.get('result_count','?')
            q=str(e.get('query','')).replace('\n',' ')[:110]
            a=str(e.get('answer',''))[:80]
            print(f'{i}. type={typ} answered={ans} rc={rc}')
            if q: print(f'   query: {q}')
            if ans and a: print(f'   answer: {a}')
asyncio.run(main())
