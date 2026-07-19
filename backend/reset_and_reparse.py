"""重置卡死需求状态并重新触发解析。"""
import urllib.request, json, urllib.error
import asyncio
from app.core.auth import get_default_token
from app.db.database import async_session
from app.db.models import Requirement, AnalysisTask
from sqlalchemy import select, update

TOKEN = get_default_token()


def api(method, path, body=None, timeout=120):
    url = f"http://localhost:8000/api{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = json.dumps(body).encode() if body else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, body_text


async def reset_requirements():
    """把卡在 analyzing 的需求重置为 draft，并取消相关任务。"""
    async with async_session() as s:
        # 查找 analyzing 状态的需求
        rs = await s.execute(
            select(Requirement).where(Requirement.status == "analyzing")
        )
        stuck = rs.scalars().all()
        print(f"找到 {len(stuck)} 个 analyzing 状态的需求:")
        for r in stuck:
            print(f"  {r.id} {r.title[:40]} (created {r.created_at})")
            # 重置为 draft
            r.status = "draft"
            # 同时把关联的 running/interrupted 任务标记为 interrupted
            ts = await s.execute(
                select(AnalysisTask).where(
                    AnalysisTask.req_id == r.id,
                    AnalysisTask.status.in_(["running", "pending", "interrupted"])
                )
            )
            for t in ts.scalars():
                t.status = "interrupted"
                print(f"    -> 任务 {t.id} 标记为 interrupted")
        await s.commit()
        print(f"已重置 {len(stuck)} 个需求为 draft")


async def main():
    await reset_requirements()

    # 选定测试需求
    test_req_id = "REQ-1784473087033-4566"

    # 用 regenerate 重新触发解析
    print(f"\n重新触发解析: POST /requirements/{test_req_id}/regenerate")
    code, data = api("POST", f"/requirements/{test_req_id}/regenerate",
                     body={"extra_notes": "无补充信息"}, timeout=60)
    print(f"  code={code}")
    if code != 200:
        print(f"  失败: {data}")
        # 尝试用 upload 重新创建
        print("\n回退方案: 重新创建需求")
        with open("/app/test_e2e_payload.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
        code, data = api("POST", "/requirements/upload", payload)
        print(f"  code={code}")
        if code == 200:
            new_req = data["data"]["req_id"]
            new_task = data["data"]["task_id"]
            print(f"  新需求: {new_req}, 任务: {new_task}")
            return new_req, new_task
        return None, None

    cd = data.get("data", {})
    new_task = cd.get("task_id")
    print(f"  新任务: {new_task}")
    return test_req_id, new_task


if __name__ == "__main__":
    req_id, task_id = asyncio.run(main())
    print(f"\n=== 结果 ===")
    print(f"req_id: {req_id}")
    print(f"task_id: {task_id}")
