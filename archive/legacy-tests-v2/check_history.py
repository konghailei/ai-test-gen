"""查询 REQ-1784473087033-4566 的历史版本"""
import requests, json
BASE = "http://localhost:8000"
r = requests.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "888888"}, timeout=15)
TOKEN = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {TOKEN}"}

# 用刚才有 regenerate 任务的 req
rid = "REQ-1784473087033-4566"
print(f"需求: {rid}")

# 查询历史版本
v = requests.get(f"{BASE}/api/requirements/{rid}/versions", headers=h, timeout=30).json()
v_data = v.get("data", v)
items_v = v_data.get("items", []) if isinstance(v_data, dict) else []
print(f"历史版本数: {len(items_v)}")
for it in items_v:
    print(f"  v{it.get('version_index')}: snapshot_at={it.get('snapshot_at')}, title={it.get('title','')[:60]}")
    print(f"    trigger={it.get('trigger')}, extra_notes={it.get('extra_notes','')[:80]}")

if items_v:
    # 查看首个版本详情
    v_idx = items_v[0].get("version_index", 1)
    v_detail = requests.get(f"{BASE}/api/requirements/{rid}/versions/{v_idx}", headers=h, timeout=30).json()
    v_detail_data = v_detail.get("data", v_detail)
    if isinstance(v_detail_data, dict):
        srr = v_detail_data.get("srr", {})
        srr_keys = list(srr.keys()) if isinstance(srr, dict) else []
        print(f"\n版本 {v_idx} 详情:")
        print(f"  srr 字段: {srr_keys}")
        if isinstance(srr, dict):
            req_block = srr.get("requirement", {})
            if req_block:
                print(f"  requirement.title: {req_block.get('title','')[:60]}")
            flows = srr.get("flows", {})
            if isinstance(flows, dict):
                hp = flows.get("happy_path", {})
                if isinstance(hp, dict):
                    steps = hp.get("steps", [])
                    print(f"  flows.happy_path.steps: {len(steps)} 步")
            fps = srr.get("functional_points", [])
            print(f"  functional_points: {len(fps)} 个")
        print(f"\n  [OK] 历史版本详情接口正常工作！")
