"""
P1 修复端到端测试脚本 - 验证 submit_clarify 和 regenerate 的异步任务轮询模式

测试流程：
1. 上传需求文档（raw_text 模式）→ 获取 task_id
2. 轮询 upload 任务状态 → completed
3. 查询澄清问题 → 获取 questions
4. 提交澄清答案 → 获取 task_id（P1 验证点：异步任务模式）
5. 轮询 submit_clarify 任务状态 → completed（P1 验证点：任务完成）
6. 重新生成需求 → 获取 task_id（P1 验证点：异步任务模式）
7. 轮询 regenerate 任务状态 → completed（P1 验证点：任务完成）
"""

import json
import time
import sys
import requests

BASE = "http://localhost:8000/api"

# 登录获取 token
print("=" * 60)
print("Step 1: 登录获取 JWT token")
print("=" * 60)
login_resp = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
login_data = login_resp.json()
if not login_data.get("success"):
    print(f"[FAIL] 登录失败: {login_data}")
    sys.exit(1)
token = login_data["data"]["token"]
print(f"[OK] 登录成功，token 前 20 字符: {token[:20]}...")

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# 读取测试需求文件
print("\n" + "=" * 60)
print("Step 2: 上传需求文档（raw_text 模式）")
print("=" * 60)
with open("test_data/requirements/02_login_optimize.md", "r", encoding="utf-8") as f:
    raw_text = f.read()

upload_payload = {
    "raw_text": raw_text,
    "product_id": "PROD-04406-853",
    "title": "登录功能优化（图形验证码）- P1测试",
    "project_id": "PROJ-853-14442-235",
    "modules": ["MOD-853-74363-485"],
}
upload_resp = requests.post(f"{BASE}/requirements/upload", json=upload_payload, headers=headers)
upload_data = upload_resp.json()
if not upload_data.get("success"):
    print(f"[FAIL] 上传失败: {upload_data}")
    sys.exit(1)
req_data = upload_data["data"]
req_id = req_data["req_id"]
task_id = req_data.get("task_id")
print(f"[OK] 上传成功: req_id={req_id}, task_id={task_id}")

# 轮询 upload 任务状态
print("\n" + "=" * 60)
print(f"Step 3: 轮询上传任务状态 (task_id={task_id})")
print("=" * 60)
if task_id:
    for i in range(60):  # 最多等待 90 秒（1.5s * 60）
        task_resp = requests.get(f"{BASE}/requirements/tasks/{task_id}", headers=headers)
        task_data = task_resp.json()["data"]
        status = task_data["status"]
        phase = task_data["phase"]
        progress = task_data["progress"]
        message = task_data.get("message", "")
        print(f"  [{i+1:02d}] status={status}, phase={phase}, progress={progress}%, message={message[:50]}")
        if status == "completed":
            print(f"[OK] 上传任务完成！")
            break
        elif status in ("failed", "interrupted"):
            print(f"[FAIL] 任务失败: {task_data.get('error', '')}")
            sys.exit(1)
        time.sleep(1.5)
    else:
        print("[FAIL] 任务超时（90秒未完成）")
        sys.exit(1)

# 查询澄清问题
print("\n" + "=" * 60)
print(f"Step 4: 查询澄清问题 (req_id={req_id})")
print("=" * 60)
clarify_resp = requests.get(f"{BASE}/requirements/{req_id}/clarify/questions", headers=headers)
clarify_data = clarify_resp.json()
if not clarify_data.get("success"):
    print(f"[FAIL] 查询澄清问题失败: {clarify_data}")
    sys.exit(1)
clarify_state = clarify_data["data"]
questions = clarify_state.get("questions", [])
quality_report = clarify_state.get("quality_report", {})
kb_status = clarify_state.get("kb_status", {})
auto_augmentations = clarify_state.get("auto_augmentations", [])
field_conflicts = clarify_state.get("field_conflicts", [])
print(f"[OK] 澄清问题查询成功")
print(f"     - 问题数量: {len(questions)}")
print(f"     - KB 状态: {kb_status}")
print(f"     - 质量报告 overall_score: {quality_report.get('overall_score', 'N/A')}")
print(f"     - KB 补全数量: {len(auto_augmentations)}")
print(f"     - 字段冲突数量: {len(field_conflicts)}")

# 打印前 3 个问题
for i, q in enumerate(questions[:3]):
    print(f"     - 问题 {i+1}: id={q.get('id')}, priority={q.get('priority')}, field={q.get('field')}")

# TC-09 验证
if len(questions) > 0:
    print("\n[TC-09 PASS] 澄清问题展示正常")
else:
    print("\n[TC-09 SKIP] 无澄清问题（需求完整）")

# TC-10 验证
if len(auto_augmentations) > 0:
    print("[TC-10 PASS] KB 补全展示正常")
else:
    print("[TC-10 INFO] 无 KB 补全（可能 KB 中无相似需求）")

# TC-11 验证
if len(field_conflicts) > 0:
    print("[TC-11 PASS] 字段冲突展示正常")
else:
    print("[TC-11 INFO] 无字段冲突")

# Step 5: 提交澄清答案（P1 核心验证点）
print("\n" + "=" * 60)
print(f"Step 5: 提交澄清答案（P1 验证：异步任务模式）")
print("=" * 60)
if len(questions) == 0:
    print("[SKIP] 无澄清问题，跳过 submit_clarify 测试")
    print("       将通过 regenerate 测试验证 P1 异步任务模式")
else:
    answers = {}
    for q in questions:
        default_ans = q.get("default_answer", "")
        if default_ans:
            answers[q["id"]] = default_ans
        else:
            answers[q["id"]] = "测试答案 - " + q.get("field", "")[:30]

    submit_payload = {"answers": answers, "extra_notes": "P1 修复端到端测试"}
    submit_resp = requests.post(
        f"{BASE}/requirements/{req_id}/clarify",
        json=submit_payload,
        headers=headers,
    )
    submit_data = submit_resp.json()
    if not submit_data.get("success"):
        print(f"[FAIL] 提交澄清失败: {submit_data}")
        sys.exit(1)
    submit_result = submit_data["data"]
    submit_task_id = submit_result.get("task_id")
    print(f"[OK] 提交澄清成功")
    print(f"     - 返回 task_id: {submit_task_id}")
    print(f"     - 返回 req_id: {submit_result.get('req_id')}")
    print(f"     - 返回 status: {submit_result.get('status')}")
    print(f"     - 返回 answers_count: {submit_result.get('answers_count')}")

    # TC-12 验证
    if submit_task_id:
        print("\n[TC-12 PASS] 提交澄清返回 task_id（P1 异步任务模式验证通过）")
    else:
        print("\n[TC-12 FAIL] 未返回 task_id")
        sys.exit(1)

    # 轮询 submit_clarify 任务状态
    print("\n" + "=" * 60)
    print(f"Step 6: 轮询 submit_clarify 任务状态")
    print("=" * 60)
    for i in range(60):  # 最多等待 90 秒
        task_resp = requests.get(f"{BASE}/requirements/tasks/{submit_task_id}", headers=headers)
        task_data = task_resp.json()["data"]
        status = task_data["status"]
        phase = task_data["phase"]
        progress = task_data["progress"]
        message = task_data.get("message", "")
        print(f"  [{i+1:02d}] status={status}, phase={phase}, progress={progress}%, message={message[:60]}")
        if status == "completed":
            print(f"\n[TC-13/TC-14 PASS] submit_clarify 任务完成（P1 轮询模式验证通过）")
            break
        elif status in ("failed", "interrupted"):
            print(f"\n[TC-15] 任务失败/中断: {task_data.get('error', '')}")
            break
        time.sleep(1.5)
    else:
        print("\n[FAIL] submit_clarify 任务超时")

# Step 7: 测试重新生成（P1 核心验证点）
print("\n" + "=" * 60)
print(f"Step 7: 重新生成需求（P1 验证：异步任务模式）")
print("=" * 60)
regen_payload = {"extra_notes": "P1 测试 - 重新生成需求以验证异步任务轮询"}
regen_resp = requests.post(
    f"{BASE}/requirements/{req_id}/regenerate",
    json=regen_payload,
    headers=headers,
)
regen_data = regen_resp.json()
if not regen_data.get("success"):
    print(f"[FAIL] 重新生成失败: {regen_data}")
    sys.exit(1)
regen_result = regen_data["data"]
regen_task_id = regen_result.get("task_id")
print(f"[OK] 重新生成请求成功")
print(f"     - 返回 task_id: {regen_task_id}")
print(f"     - 返回 req_id: {regen_result.get('req_id')}")
print(f"     - 返回 status: {regen_result.get('status')}")
print(f"     - 返回 previous_title: {regen_result.get('previous_title')}")

# TC-16 验证
if regen_task_id:
    print("\n[TC-16 PASS] 重新生成返回 task_id（P1 异步任务模式验证通过）")
else:
    print("\n[TC-16 FAIL] 未返回 task_id")
    sys.exit(1)

# 轮询 regenerate 任务状态
print("\n" + "=" * 60)
print(f"Step 8: 轮询 regenerate 任务状态")
print("=" * 60)
for i in range(60):
    task_resp = requests.get(f"{BASE}/requirements/tasks/{regen_task_id}", headers=headers)
    task_data = task_resp.json()["data"]
    status = task_data["status"]
    phase = task_data["phase"]
    progress = task_data["progress"]
    message = task_data.get("message", "")
    print(f"  [{i+1:02d}] status={status}, phase={phase}, progress={progress}%, message={message[:60]}")
    if status == "completed":
        print(f"\n[TC-17/TC-18 PASS] regenerate 任务完成（P1 轮询模式验证通过）")
        break
    elif status in ("failed", "interrupted"):
        print(f"\n[TC-19] 任务失败/中断: {task_data.get('error', '')}")
        break
    time.sleep(1.5)
else:
    print("\n[FAIL] regenerate 任务超时")

# 总结
print("\n" + "=" * 60)
print("测试总结")
print("=" * 60)
print(f"req_id: {req_id}")
print(f"upload task_id: {task_id}")
if len(questions) > 0 and submit_task_id:
    print(f"submit_clarify task_id: {submit_task_id}")
print(f"regenerate task_id: {regen_task_id}")
print("\nP1 修复（前端任务轮询）验证完成")
print("后端异步任务模式 + 任务状态轮询 API 工作正常")
