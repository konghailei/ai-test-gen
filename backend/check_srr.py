import asyncio
import json
import sys
sys.path.insert(0, '/app')

from app.db.database import async_session
from app.db.models import Requirement


async def main():
    async with async_session() as s:
        r = await s.get(Requirement, 'REQ-1784693315190-0386')
        if not r:
            print('NOT FOUND')
            return
        srr = r.srr_json if r.srr_json else {}
        print('status:', r.status)
        print('SRR keys:', list(srr.keys()) if srr else 'EMPTY')
        print('requirement:', json.dumps(srr.get('requirement', {}), ensure_ascii=False)[:300])
        flows = srr.get('flows', {})
        print('flows type:', type(flows).__name__)
        if isinstance(flows, dict):
            hp = flows.get('happy_path', {})
            print('happy_path type:', type(hp).__name__)
            if isinstance(hp, dict):
                print('happy_path steps count:', len(hp.get('steps', [])))
        print('cross_fp_flows count:', len(srr.get('cross_fp_flows', [])))
        print('fp_contracts count:', len(srr.get('fp_contracts', [])))
        print('shared_business_rules count:', len(srr.get('shared_business_rules', [])))
        print('fp_index count:', len(srr.get('fp_index', [])))
        print('global_constraints:', srr.get('global_constraints', []))
        print('_clarify_state:', srr.get('_clarify_state', 'NONE'))
        print('_kb_query_logs:', len(srr.get('_kb_query_logs', [])))


asyncio.run(main())
