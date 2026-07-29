"""直接调用导出函数验证（不通过 HTTP）。"""
import json
import zipfile
import io
import asyncio
from sqlalchemy import select
from app.db.database import async_session
from app.db.models import Requirement, TestCase
from app.exporters.xmind import export_to_xmind

async def verify():
    async with async_session() as session:
        # 获取所有用例
        tc_result = await session.execute(select(TestCase).limit(20))
        cases_rows = tc_result.scalars().all()
        print(f"[用例] 共 {len(cases_rows)} 个")

        if not cases_rows:
            print("[跳过] 无用例")
            return

        # 获取关联需求标题
        req_ids = set(c.req_id for c in cases_rows)
        req_result = await session.execute(select(Requirement).where(Requirement.id.in_(req_ids)))
        req_map = {r.id: r.title for r in req_result.scalars().all()}

        # 构造导出数据
        cases_data = []
        for c in cases_rows:
            cj = c.case_json or {}
            fp = cj.get("related_fp") or cj.get("functional_point") or ""
            cases_data.append({
                "id": c.id,
                "req_id": c.req_id,
                "req_title": req_map.get(c.req_id, ""),
                "title": c.title,
                "type": c.type,
                "priority": c.priority,
                "functional_point": fp,
                "preconditions": cj.get("preconditions", []),
                "steps": cj.get("steps", []),
            })

    # 调用导出函数
    print("\n[导出] 调用 export_to_xmind...")
    xmind_bytes = export_to_xmind(cases_data, "所有用例")
    print(f"[导出] 文件大小: {len(xmind_bytes)} bytes")

    # 验证 ZIP
    with zipfile.ZipFile(io.BytesIO(xmind_bytes)) as zf:
        names = zf.namelist()
        print(f"[导出] ZIP 内文件: {names}")
        if "content.json" in names:
            content = json.loads(zf.read("content.json").decode("utf-8"))
            root = content[0]["rootTopic"]
            print(f"[导出] 根节点: {root.get('title', '')}")
            level1 = root.get("children", {}).get("attached", [])
            for n1 in level1[:3]:
                print(f"  一级: {n1.get('title', '')}")
                level2 = n1.get("children", {}).get("attached", [])
                for n2 in level2[:3]:
                    print(f"    二级: {n2.get('title', '')}")
                if len(level2) > 3:
                    print(f"    ... 共 {len(level2)} 个")
            print(f"\n✓ 导出成功，{len(cases_data)} 个用例，ZIP 有效")

    # 验证按需求筛选导出
    if cases_data:
        first_req_id = cases_data[0]["req_id"]
        req_cases = [c for c in cases_data if c["req_id"] == first_req_id]
        print(f"\n[按需求导出] 需求 {first_req_id} 下 {len(req_cases)} 个用例")
        xmind_bytes2 = export_to_xmind(req_cases, first_req_id)
        print(f"[按需求导出] 文件大小: {len(xmind_bytes2)} bytes")
        with zipfile.ZipFile(io.BytesIO(xmind_bytes2)) as zf:
            content = json.loads(zf.read("content.json").decode("utf-8"))
            root = content[0]["rootTopic"]
            print(f"[按需求导出] 根节点: {root.get('title', '')}")
            level1 = root.get("children", {}).get("attached", [])
            for n1 in level1[:3]:
                print(f"  一级: {n1.get('title', '')}")
        print("✓ 只选需求（不选项目）时导出成功")

asyncio.run(verify())
