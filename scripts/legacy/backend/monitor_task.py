"""监控任务进度直到完成或超时。"""
import time, json, urllib.request, urllib.error
from app.core.auth import get_default_token
TOKEN = get_default_token()


def api(path, timeout=30):
    url = f"http://localhost:8000/api{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    task_id = "TASK-1784473446866-1767"
    req_id = "REQ-1784473087033-4566"
    max_wait = 600  # 10 分钟
    start = time.time()
    last_phase = None
    while time.time() - start < max_wait:
        code, data = api(f"/requirements/tasks/{task_id}")
        if code != 200:
            print(f"查询失败: {code}")
            time.sleep(5)
            continue
        t = data.get("data", {})
        status = t.get("status")
        phase = t.get("phase")
        progress = t.get("progress", 0)
        msg = t.get("message", "")
        if phase != last_phase or progress % 20 == 0:
            print(f"[{time.strftime('%H:%M:%S')}] status={status} phase={phase} progress={progress}% {msg[:60]}")
            last_phase = phase
        if status in ("completed", "success", "failed", "interrupted"):
            print(f"\n=== 任务结束: status={status} ===")
            print(f"message: {msg}")
            if t.get("error"):
                print(f"error: {t['error']}")
            if t.get("result_json"):
                rj = t["result_json"]
                if isinstance(rj, str):
                    try:
                        rj = json.loads(rj)
                    except Exception:
                        pass
                print(f"result_json keys: {list(rj.keys()) if isinstance(rj, dict) else type(rj)}")
            break
        time.sleep(5)
    else:
        print(f"\n超时（{max_wait}s）")

    # 查询需求状态
    code, data = api(f"/requirements/{req_id}")
    rd = data.get("data", {})
    print(f"\n=== 需求最终状态 ===")
    print(f"req_id: {rd.get('id')}")
    print(f"status: {rd.get('status')}")
    print(f"title: {rd.get('title')}")
    srr = rd.get("srr_json") or {}
    if isinstance(srr, dict):
        req = srr.get("requirement", {})
        print(f"modules: {req.get('modules', [])}")
        print(f"priority: {req.get('priority')}")
        fps = req.get("functional_points", [])
        print(f"functional_points: {len(fps)}")
        for fp in fps[:10]:
            if isinstance(fp, dict):
                print(f"  - [{fp.get('fp_type','?')}] {fp.get('name','?')} (module={fp.get('module_id','?')})")
        bp = req.get("business_points", [])
        print(f"business_points: {len(bp)}")
        for b in bp[:5]:
            if isinstance(b, dict):
                print(f"  - {b.get('name','?')}")
        qr = srr.get("quality_report", {})
        print(f"quality_report: overall={qr.get('overall_score')} completeness={qr.get('completeness_score')} ambiguity={qr.get('ambiguity_score')}")
        print(f"summary: {qr.get('summary','')[:100]}")
        flows = req.get("flows", {})
        if isinstance(flows, dict):
            print(f"flows.steps: {len(flows.get('steps', []))}")
        dc = req.get("data_constraints", [])
        print(f"data_constraints: {len(dc) if isinstance(dc, list) else 'n/a'}")


if __name__ == "__main__":
    main()
