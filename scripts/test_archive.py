"""E2E 测试：归档需求，验证 Celery 异步双写。"""
import json
import sys
import time

import requests

API_BASE = "http://localhost:8000"
REQ_ID = sys.argv[1] if len(sys.argv) > 1 else "REQ-1783838540411-9076"


def login() -> str:
    resp = requests.post(
        f"{API_BASE}/api/auth/login",
        json={"username": "admin", "password": "888888"},
        timeout=15,
    )
    return resp.json()["data"]["token"]


def archive(token: str, req_id: str):
    headers = {"Authorization": f"Bearer {token}"}
    body = {"go_live_date": "2026-07-12"}
    resp = requests.post(
        f"{API_BASE}/api/requirements/{req_id}/archive",
        json=body,
        headers=headers,
        timeout=30,
    )
    print(f"[Archive] HTTP {resp.status_code}")
    data = resp.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main():
    print(f"=== E2E 测试：归档需求 req_id={REQ_ID} ===")
    token = login()
    print(f"[OK] 登录成功")

    t0 = time.time()
    result = archive(token, REQ_ID)
    t1 = time.time()
    print(f"\n归档耗时: {t1 - t0:.2f}s")

    if result.get("success"):
        kb_task_queued = result.get("data", {}).get("kb_task_queued", False)
        print(f"\n=== 归档结果 ===")
        print(f"  kb_task_queued: {kb_task_queued}")
        print(f"  go_live_date: {result.get('data', {}).get('go_live_date')}")
        print(f"  project_version: {result.get('data', {}).get('project_version')}")

        if kb_task_queued:
            print(f"\n[OK] Celery 异步任务已派发！等待 10 秒后检查 Celery worker 日志...")
            time.sleep(10)
            print(f"\n请检查 Celery 日志: docker logs ai-test-gen-celery --tail 20")
        else:
            print(f"\n[WARN] KB 写入失败: {result.get('data', {}).get('kb_error')}")


if __name__ == "__main__":
    main()
