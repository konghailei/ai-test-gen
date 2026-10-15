"""快速查询历史版本状态"""
import requests
BASE = "http://localhost:8000"
r = requests.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "888888"}, timeout=15)
TOKEN = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {TOKEN}"}

# 找到 active 需求
r = requests.get(f"{BASE}/api/requirements?page=1&page_size=50", headers=h, timeout=30)
items = r.json().get("data", {}).get("items", [])
print(f"总需求数: {len(items)}")
for x in items[:10]:
    print(f"  {x['id']} | status={x.get('status')} | title={x.get('title','')[:40]}")

active = next((x for x in items if x.get("status") in ("active", "confirmed")), None)
if not active:
    print("无 active 需求")
    raise SystemExit(0)

rid = active["id"]
print(f"\n使用需求: {rid}, 状态: {active['status']}")

# 查询历史版本
v = requests.get(f"{BASE}/api/requirements/{rid}/versions", headers=h, timeout=30).json()
v_data = v.get("data", v)
items_v = v_data.get("items", []) if isinstance(v_data, dict) else []
print(f"历史版本数: {len(items_v)}")
for it in items_v:
    print(f"  v{it.get('version_index')}: snapshot_at={it.get('snapshot_at')}, title={it.get('title','')[:50]}, trigger={it.get('trigger')}")

# 查询最近的任务状态
print("\n最近任务状态:")
r = requests.get(f"{BASE}/api/requirements/tasks?limit=5", headers=h, timeout=30)
tasks = r.json().get("data", {}).get("items", [])
for t in tasks[:5]:
    print(f"  {t.get('id')} | type={t.get('task_type')} | status={t.get('status')} | req={t.get('req_id')} | phase={t.get('phase')} | progress={t.get('progress')} | msg={t.get('message','')[:60]}")
