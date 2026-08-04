"""测试脚本：重新触发需求分析，验证去重和qa_records修复"""
import asyncio
import json
import time
import httpx

BASE_URL = "http://localhost:8000/api"
REQ_ID = "REQ-1784855029523-4306"


async def main():
    # 1. 登录
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{BASE_URL}/auth/login", json={"username": "admin", "password": "888888"})
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"✅ 登录成功")

        # 2. 查看当前需求状态
        resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}", headers=headers)
        req_data = resp.json()
        req = req_data.get("data", req_data)
        print(f"\n需求状态: status={req.get('status')} version={req.get('version')}")

        # 3. 查看当前澄清历史
        resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}/clarify-history", headers=headers)
        hist_data = resp.json()
        history = hist_data.get("data", hist_data)
        if isinstance(history, list):
            print(f"当前澄清历史条数: {len(history)}")
            for i, h in enumerate(history):
                qa_records = h.get("qa_records", [])
                questions = h.get("questions", [])
                print(f"  [{i}] action={h.get('action')} qa_records={len(qa_records)} questions={len(questions)}")

        # 4. 重新触发需求分析（regenerate）
        print(f"\n▶ 重新触发需求分析（regenerate）...")
        resp = await client.post(
            f"{BASE_URL}/requirements/{REQ_ID}/regenerate",
            headers=headers,
            json={"extra_notes": "测试去重和qa_records修复"},
        )
        regen_data = resp.json()
        print(f"regenerate 响应: {json.dumps(regen_data, ensure_ascii=False)[:300]}")
        task_id = regen_data.get("data", {}).get("task_id", "")
        if not task_id:
            print(f"❌ 未获取 task_id，响应: {json.dumps(regen_data, ensure_ascii=False)[:500]}")
            return

        # 5. 轮询任务状态
        print(f"\n▶ 轮询任务状态: task_id={task_id}")
        for i in range(120):  # 最多等 10 分钟
            await asyncio.sleep(5)
            resp = await client.get(f"{BASE_URL}/tasks/{task_id}", headers=headers)
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

        # 6. 检查结果
        if status == "failed":
            error = task.get("error", "")
            print(f"\n❌ 任务失败: {error[:300]}")
            return

        # 7. 检查生成的澄清问题是否有重复
        result_json = task.get("result_json", {}) or {}
        questions = result_json.get("questions", []) or []
        print(f"\n✅ 任务完成")
        print(f"生成的澄清问题数: {len(questions)}")

        if questions:
            seen_fields = {}
            seen_ids = {}
            for q in questions:
                fid = q.get("id", "")
                field = q.get("field", "")
                seen_fields[field] = seen_fields.get(field, 0) + 1
                seen_ids[fid] = seen_ids.get(fid, 0) + 1
            dup_fields = {k: v for k, v in seen_fields.items() if v > 1}
            dup_ids = {k: v for k, v in seen_ids.items() if v > 1}
            if dup_fields:
                print(f"  ⚠️ 重复 field: {dup_fields}")
            if dup_ids:
                print(f"  ⚠️ 重复 id: {dup_ids}")
            if not dup_fields and not dup_ids:
                print(f"  ✅ 无重复问题")
            for i, q in enumerate(questions):
                print(f"    [{i}] id={q.get('id', '')[:30]} field={q.get('field', '')} Q={(q.get('question', '') or '')[:70]}")
        else:
            print("  ℹ️ 无澄清问题（可能质量已达标直接通过）")

        # 8. 如果有澄清问题，提交答案测试 qa_records
        if questions:
            print(f"\n▶ 提交澄清答案测试 qa_records...")
            answers = {}
            for q in questions:
                qid = q.get("id", "")
                default_answer = q.get("default_answer", "") or ""
                options = q.get("options", []) or []
                if options:
                    answers[qid] = options[0]
                else:
                    answers[qid] = default_answer or "测试答案"
            print(f"  提交答案: {json.dumps(answers, ensure_ascii=False)[:300]}")

            resp = await client.post(
                f"{BASE_URL}/requirements/{REQ_ID}/clarify",
                headers=headers,
                json={"answers": answers, "extra_notes": "测试qa_records"},
            )
            clarify_data = resp.json()
            print(f"  clarify 响应: {json.dumps(clarify_data, ensure_ascii=False)[:300]}")
            clarify_task_id = clarify_data.get("data", {}).get("task_id", "")

            if clarify_task_id:
                # 轮询 clarify 任务
                for i in range(60):
                    await asyncio.sleep(5)
                    resp = await client.get(f"{BASE_URL}/tasks/{clarify_task_id}", headers=headers)
                    task_data = resp.json()
                    task = task_data.get("data", task_data)
                    status = task.get("status", "")
                    phase = task.get("phase", "")
                    progress = task.get("progress", 0)
                    print(f"    [{i*5}s] status={status} phase={phase} progress={progress}%")
                    if status in ("completed", "failed"):
                        break

                # 检查澄清历史
                resp = await client.get(f"{BASE_URL}/requirements/{REQ_ID}/clarify-history", headers=headers)
                hist_data = resp.json()
                history = hist_data.get("data", hist_data)
                if isinstance(history, list):
                    print(f"\n澄清历史条数: {len(history)}")
                    for i, h in enumerate(history):
                        qa_records = h.get("qa_records", [])
                        questions_h = h.get("questions", [])
                        action = h.get("action", "")
                        print(f"  [{i}] action={action} qa_records={len(qa_records)} questions={len(questions_h)}")
                        if qa_records:
                            for qa in qa_records[:3]:
                                print(f"        - id={qa.get('id', '')[:25]} field={qa.get('field', '')}")
                                print(f"          Q: {(qa.get('question', '') or '')[:80]}")
                                print(f"          A: {(qa.get('answer', '') or '')[:80]}")
                            if len(qa_records) > 0:
                                print(f"  ✅ qa_records 已记录")
                        elif action == "clarify":
                            print(f"  ⚠️ 无 qa_records")


if __name__ == "__main__":
    asyncio.run(main())
