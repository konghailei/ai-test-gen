"""检查现有需求状态并准备继续测试。"""
import urllib.request, json, urllib.error
from app.core.auth import get_default_token
TOKEN = get_default_token()


def api(method, path, body=None):
    url = f"http://localhost:8000/api{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = json.dumps(body).encode() if body else None
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# 1. 取消卡死的任务
code, data = api("POST", "/requirements/tasks/TASK-1784473087033-4467/cancel")
print(f"取消卡死任务: code={code} status={data.get('data', {}).get('status') if isinstance(data.get('data'), dict) else data}")

# 2. 查询最近 10 个需求，看哪些是 confirmed 可用的
code, data = api("GET", "/requirements?page=1&page_size=10")
items = data.get("data", {}).get("items", [])
print(f"\n最近 10 个需求:")
for r in items:
    print(f"  {r['id']} [{r['status']}] {r.get('title','')[:50]}")

# 3. 重点查看 REQ-1784473250697-4567 (新解析的)
code, data = api("GET", "/requirements/REQ-1784473250697-4567")
rd = data.get("data", {})
print(f"\n=== 新需求详情 ===")
print(f"req_id: {rd.get('id')}")
print(f"状态: {rd.get('status')}")
print(f"标题: {rd.get('title')}")
srr = rd.get("srr_json") or {}
if isinstance(srr, dict):
    req = srr.get("requirement", {})
    print(f"模块: {req.get('modules', [])}")
    print(f"优先级: {req.get('priority')}")
    fps = req.get("functional_points", [])
    print(f"功能点数: {len(fps)}")
    for fp in fps[:10]:
        if isinstance(fp, dict):
            print(f"  - [{fp.get('fp_type','?')}] {fp.get('name','?')} (module={fp.get('module_id','?')})")
    bp = req.get("business_points", [])
    print(f"业务点数: {len(bp)}")
    qr = srr.get("quality_report", {})
    print(f"质量评分: overall={qr.get('overall_score')} completeness={qr.get('completeness_score')} ambiguity={qr.get('ambiguity_score')}")
    print(f"质量摘要: {qr.get('summary','')[:80]}")
