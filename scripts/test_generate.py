"""E2E 测试脚本：调用 SSE 接口生成测试用例，收集所有事件并打印关键节点。

使用 requests 库实现可靠的 SSE 流式读取。
"""
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


def generate(token: str, req_id: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    t0 = time.time()
    events = []
    with requests.post(
        f"{API_BASE}/api/generate",
        json={"req_id": req_id},
        headers=headers,
        stream=True,
        timeout=600,
    ) as resp:
        print(f"[INFO] HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"[ERROR] {resp.text[:500]}")
            return []
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                try:
                    evt = json.loads(line[5:].strip())
                    events.append(evt)
                    stage = evt.get("stage", "?")
                    msg = str(evt.get("message", ""))[:120]
                    progress = evt.get("progress", 0)
                    elapsed = time.time() - t0
                    print(f"[{elapsed:6.1f}s][{stage:12s}][{progress:3d}%] {msg}")
                    if stage in ("error", "done"):
                        return events
                except json.JSONDecodeError:
                    pass
    return events


def main():
    print(f"=== E2E 测试：生成测试用例 req_id={REQ_ID} ===")
    token = login()
    print(f"[OK] 登录成功，token 长度={len(token)}")
    events = generate(token, REQ_ID)
    print(f"\n=== 共收到 {len(events)} 个事件 ===")

    done_evts = [e for e in events if e.get("stage") == "done"]
    err_evts = [e for e in events if e.get("stage") == "error"]
    if done_evts:
        d = done_evts[-1]
        print(f"\n=== DONE 事件 ===")
        print(f"  req_id: {d.get('req_id')}")
        print(f"  channel: {d.get('channel')}")
        print(f"  message: {d.get('message')}")
        if "reviewed_cases" in d:
            tcs = d["reviewed_cases"]
            print(f"  reviewed_cases_count: {len(tcs)}")
            for i, tc in enumerate(tcs[:10], 1):
                title = tc.get("title", "")[:50]
                print(f"    [{i}] id={tc.get('id')} title={title} "
                      f"priority={tc.get('priority')} type={tc.get('type')} "
                      f"confidence={tc.get('confidence')}")
            if len(tcs) > 10:
                print(f"    ... 还有 {len(tcs) - 10} 条")
        if "review_report" in d:
            rpt = d["review_report"]
            print(f"  review_report: total={rpt.get('total_cases')} "
                  f"passed={rpt.get('passed_cases')} blocked={rpt.get('blocked_cases')} "
                  f"avg_conf={rpt.get('avg_confidence')}")
    if err_evts:
        print(f"\n=== ERROR 事件 ===")
        for e in err_evts:
            print(f"  {e}")

    out_path = f"e:/git-down/ai-test-gen/logs/generate_{REQ_ID}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[OK] 完整事件已写入 {out_path}")


if __name__ == "__main__":
    main()
