"""端到端检测脚本：需求分析 + 归档 + KB 联动 + 影响分析 + 用例生成 5 个优化。

使用方法：
    docker exec -i ai-test-gen-api python - < test_data/detect_full_flow.py

环境变量：
    BASE_URL: 后端地址（默认 http://localhost:8000）
"""
import os
import sys
import json
import time
import requests

# 关闭 stdout 缓冲，确保实时输出
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "888888")

print(f"[detect] BASE_URL={BASE_URL}")
print(f"[detect] ADMIN_USER={ADMIN_USER}")
print("=" * 70)


def header(token: str | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    token = data["data"]["token"]
    print(f"[login] OK, token={token[:20]}...")
    return token


def upload_requirement(token: str, raw_text: str, product_id: str = "P-DETECT-001") -> dict:
    """上传需求（异步任务模式，立即返回 task_id）。"""
    r = requests.post(
        f"{BASE_URL}/api/requirements/upload",
        headers=header(token),
        json={"raw_text": raw_text, "product_id": product_id, "title": f"检测需求-{int(time.time())}"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()["data"]
    print(f"[upload] OK: task_id={data.get('task_id')}, req_id={data.get('req_id')}, status={data.get('status')}")
    return data


def wait_analysis_done(token: str, task_id: str, timeout: int = 180) -> dict:
    """轮询分析任务状态，直到完成。"""
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(
            f"{BASE_URL}/api/requirements/tasks/{task_id}",
            headers=header(token),
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()["data"]
            status = data.get("status", "")
            progress = data.get("progress", 0)
            stage = data.get("current_stage", "")
            print(f"[poll] status={status}, progress={progress}%, stage={stage}")
            if status == "completed":
                return data
            if status == "failed":
                print(f"[poll] FAILED: {data.get('error_message', '')}")
                return data
        time.sleep(3)
    print(f"[poll] TIMEOUT after {timeout}s")
    return {}


def get_requirement_detail(token: str, req_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/api/requirements/{req_id}",
        headers=header(token),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["data"]


def finalize_requirement(token: str, req_id: str) -> dict:
    """完成需求澄清（draft → active），归档前置条件。"""
    r = requests.post(
        f"{BASE_URL}/api/requirements/{req_id}/finalize",
        headers=header(token),
        timeout=60,
    )
    print(f"[finalize] status_code={r.status_code}")
    try:
        data = r.json()
        print(f"[finalize] response: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data
    except Exception:
        print(f"[finalize] raw response: {r.text[:500]}")
        return {}


def archive_requirement(token: str, req_id: str) -> dict:
    """归档需求（触发 KB 写入）。"""
    r = requests.post(
        f"{BASE_URL}/api/requirements/{req_id}/archive",
        headers=header(token),
        timeout=30,
    )
    print(f"[archive] status_code={r.status_code}")
    try:
        data = r.json()
        print(f"[archive] response: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data
    except Exception:
        print(f"[archive] raw response: {r.text[:500]}")
        return {}


def generate_cases(token: str, req_id: str) -> dict:
    """触发用例生成（SSE 流式）。"""
    r = requests.post(
        f"{BASE_URL}/api/generate",
        headers=header(token),
        json={"req_id": req_id, "channel": "FULL"},
        stream=True,
        timeout=300,
    )
    print(f"[generate] status_code={r.status_code}")
    last_event = {}
    for line in r.iter_lines():
        if not line:
            continue
        line_s = line.decode("utf-8", errors="ignore")
        if line_s.startswith("data:"):
            try:
                evt = json.loads(line_s[5:].strip())
                last_event = evt
                evt_type = evt.get("event", "")
                progress = evt.get("progress", "")
                print(f"[generate-sse] event={evt_type}, progress={progress}")
            except Exception:
                pass
    return last_event


def main():
    # === Step 1: 登录 ===
    print("\n### Step 1: 登录")
    token = login()

    # === Step 2: 上传需求（第一次） ===
    print("\n### Step 2: 上传需求（第一次，用于归档+KB）")
    raw_text_1 = """
【需求标题】用户登录功能
【需求描述】
用户通过账号密码登录系统，支持邮箱和手机号登录。
登录失败 5 次后锁定账号 30 分钟。
登录成功后跳转到首页。
【功能点】
1. 账号密码登录
2. 邮箱登录
3. 手机号登录
4. 登录失败锁定
"""
    upload_data_1 = upload_requirement(token, raw_text_1)
    req_id_1 = upload_data_1.get("req_id")
    task_id_1 = upload_data_1.get("task_id")
    if not req_id_1 or not task_id_1:
        print(f"[FAIL] 上传返回缺少 req_id/task_id: {upload_data_1}")
        return
    print(f"[upload-1] req_id={req_id_1}, task_id={task_id_1}")

    # === Step 3: 等待分析完成 ===
    print("\n### Step 3: 等待分析完成")
    analysis_result = wait_analysis_done(token, task_id_1, timeout=180)
    if analysis_result.get("status") != "completed":
        print(f"[FAIL] 分析未完成: {analysis_result}")
        return
    print(f"[analysis] 完成: progress={analysis_result.get('progress')}%")

    # === Step 4: 查询需求详情，验证字段完整性 ===
    print("\n### Step 4: 查询需求详情")
    detail = get_requirement_detail(token, req_id_1)
    print(f"[detail] id={detail.get('id')}")
    print(f"[detail] status={detail.get('status')}")
    print(f"[detail] vectorization_status={detail.get('vectorization_status')}")
    print(f"[detail] go_live_date={detail.get('go_live_date')}")
    print(f"[detail] archived_at={detail.get('archived_at')}")
    print(f"[detail] product_id={detail.get('product_id')}")

    # === Step 5: 完成澄清 + 归档需求（触发 KB 写入） ===
    print("\n### Step 5: 完成澄清（draft → active）")
    finalize_result = finalize_requirement(token, req_id_1)

    print("\n### Step 5.1: 归档需求（第一次）")
    archive_result_1 = archive_requirement(token, req_id_1)

    # 验证幂等性：重复归档应返回 409
    print("\n### Step 5.2: 验证归档幂等性（重复归档应返回 409）")
    archive_result_2 = archive_requirement(token, req_id_1)

    # === Step 6: 上传第二个需求（用于验证 KB 联动 + 影响分析） ===
    print("\n### Step 6: 上传需求（第二次，验证 KB 联动）")
    raw_text_2 = """
【需求标题】用户登录功能优化
【需求描述】
在原有登录功能基础上，新增以下优化：
1. 支持第三方登录（微信、QQ）
2. 登录失败锁定时间改为可配置（默认 30 分钟）
3. 新增图形验证码（登录失败 3 次后触发）
【变更点】
- 修改：登录失败锁定逻辑
- 新增：第三方登录、图形验证码
"""
    upload_data_2 = upload_requirement(token, raw_text_2, product_id="P-DETECT-001")
    req_id_2 = upload_data_2.get("req_id")
    task_id_2 = upload_data_2.get("task_id")
    print(f"[upload-2] req_id={req_id_2}, task_id={task_id_2}")

    print("\n### Step 7: 等待第二个需求分析完成")
    analysis_result_2 = wait_analysis_done(token, task_id_2, timeout=180)
    if analysis_result_2.get("status") != "completed":
        print(f"[FAIL] 第二个需求分析未完成: {analysis_result_2}")
    else:
        print(f"[analysis-2] 完成: progress={analysis_result_2.get('progress')}%")

    # === Step 8: 完成澄清 + 归档第二个需求 ===
    print("\n### Step 8: 完成澄清第二个需求（draft → active）")
    finalize_result_2 = finalize_requirement(token, req_id_2)

    print("\n### Step 8.1: 归档第二个需求")
    archive_result_3 = archive_requirement(token, req_id_2)

    # === Step 9: 触发用例生成（验证 5 个优化） ===
    print("\n### Step 9: 触发用例生成（验证 5 个优化）")
    gen_result_1 = generate_cases(token, req_id_1)
    print(f"[generate-1] 最终事件: {json.dumps(gen_result_1, ensure_ascii=False)[:300]}")

    # === Step 10: 再次生成用例（验证优化 2：旧用例清理） ===
    print("\n### Step 10: 再次生成用例（验证优化 2：旧用例清理）")
    gen_result_2 = generate_cases(token, req_id_1)
    print(f"[generate-2] 最终事件: {json.dumps(gen_result_2, ensure_ascii=False)[:300]}")

    print("\n" + "=" * 70)
    print("[detect] 全部检测步骤完成。请检查后端日志确认 5 个优化生效：")
    print("  docker logs ai-test-gen-api --tail 200 2>&1 | findstr /C:\"GenAgent\" /C:\"cache\" /C:\"delete_cases\" /C:\"向量库写入\"")


if __name__ == "__main__":
    main()
