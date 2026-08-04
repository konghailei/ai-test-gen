"""测试 qa_records 修复：直接提交澄清答案"""
import asyncio
import json
import httpx

BASE_URL = "http://localhost:8000/api"
REQ_ID = "REQ-1784855029523-4306"


async def main():
    async with httpx.AsyncClient(timeout=600) as client:
        # 1. 登录
        resp = await client.post(f"{BASE_URL}/auth/login", json={"username": "admin", "password": "888888"})
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("✅ 登录成功")

        # 2. 查看当前澄清历史
        resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}/clarify-history", headers=headers)
        hist_data = resp.json()
        history = hist_data.get("data", hist_data)
        if isinstance(history, list):
            print(f"\n当前澄清历史条数: {len(history)}")
            for i, h in enumerate(history):
                qa_records = h.get("qa_records", [])
                questions = h.get("questions", [])
                print(f"  [{i}] action={h.get('action')} qa_records={len(qa_records)} questions={len(questions)}")

        # 3. 获取当前需求详情，查看 _clarify_state
        resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}", headers=headers)
        req_data = resp.json()
        req = req_data.get("data", req_data)
        print(f"\n需求状态: {req.get('status')}")

        # 4. 从最近任务获取问题（模拟前端获取问题的流程）
        resp = await client.get(f"{BASE_URL}/tasks?req_id={REQ_ID}&limit=5", headers=headers)
        tasks_data = resp.json()
        tasks = tasks_data.get("data", {}).get("items", tasks_data.get("data", []))
        if isinstance(tasks, dict):
            tasks = tasks.get("items", [])

        questions = []
        for t in (tasks if isinstance(tasks, list) else []):
            if t.get("task_type") == "upload_requirement":
                rj = t.get("result_json", {}) or {}
                qs = rj.get("questions", []) or []
                if qs:
                    questions = qs
                    print(f"\n从任务 {t.get('id')} 找到 {len(qs)} 个问题")
                    break

        if not questions:
            print("❌ 未找到澄清问题")
            return

        # 检查重复
        seen_ids = {}
        for q in questions:
            qid = q.get("id", "")
            seen_ids[qid] = seen_ids.get(qid, 0) + 1
        dup_ids = {k: v for k, v in seen_ids.items() if v > 1}
        if dup_ids:
            print(f"⚠️ 存储的问题中有重复 id: {dup_ids}")
            print("  （这些是旧代码生成的问题，当前代码已有去重逻辑）")

        # 5. 提交澄清答案
        answers = {}
        for q in questions:
            qid = q.get("id", "")
            answers[qid] = "测试答案_" + qid
        print(f"\n▶ 提交澄清答案: {json.dumps(answers, ensure_ascii=False)[:200]}")

        resp = await client.post(
            f"{BASE_URL}/requirements/{REQ_ID}/clarify",
            headers=headers,
            json={"answers": answers, "extra_notes": "测试qa_records修复"},
        )
        clarify_data = resp.json()
        print(f"clarify 响应: {json.dumps(clarify_data, ensure_ascii=False)[:300]}")
        clarify_task_id = clarify_data.get("data", {}).get("task_id", "")

        if not clarify_task_id:
            print(f"❌ 未获取 task_id")
            return

        # 6. 轮询 clarify 任务
        print(f"\n▶ 轮询 clarify 任务: {clarify_task_id}")
        for i in range(120):
            await asyncio.sleep(5)
            resp = await client.get(f"{BASE_URL}/tasks/{clarify_task_id}", headers=headers)
            task_data = resp.json()
            task = task_data.get("data", task_data)
            status = task.get("status", "")
            phase = task.get("phase", "")
            progress = task.get("progress", 0)
            message = task.get("message", "")
            print(f"  [{i*5}s] status={status} phase={phase} progress={progress}% msg={message[:80]}")
            if status in ("completed", "failed"):
                break
        else:
            print("⏰ 超时")
            return

        if status == "failed":
            error = task.get("error", "")
            print(f"\n❌ 任务失败: {error[:300]}")
            return

        # 7. 检查澄清历史是否记录了 qa_records
        print(f"\n✅ 任务完成，检查澄清历史...")
        resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}/clarify-history", headers=headers)
        hist_data = resp.json()
        history = hist_data.get("data", hist_data)
        if isinstance(history, list):
            print(f"澄清历史条数: {len(history)}")
            for i, h in enumerate(history):
                qa_records = h.get("qa_records", [])
                questions_h = h.get("questions", [])
                action = h.get("action", "")
                print(f"\n  [{i}] action={action} qa_records={len(qa_records)} questions={len(questions_h)}")
                if qa_records:
                    print(f"  ✅ qa_records 已记录！")
                    for qa in qa_records[:5]:
                        print(f"    - id={qa.get('id', '')[:30]} field={qa.get('field', '')}")
                        print(f"      Q: {(qa.get('question', '') or '')[:80]}")
                        print(f"      A: {(qa.get('answer', '') or '')[:80]}")
                elif action == "clarify":
                    print(f"  ⚠️ 无 qa_records（修复未生效？）")
                    if questions_h:
                        print(f"     但有 questions: {len(questions_h)}")


if __name__ == "__main__":
    asyncio.run(main())
