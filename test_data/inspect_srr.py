"""快速检查 SRR 结构和 FP 状态。"""
import json
import requests

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"})
t = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {t}"}

r1 = requests.get(f"{BASE}/requirements/REQ-1784205007538-5997", headers=h)
d = r1.json().get("data", {})
srr = d.get("srr_json", {}) or {}

print("=== SRR 结构 ===")
print(f"top keys: {list(srr.keys())}")
print()

# flows (可能是 dict 或 list)
flows = srr.get("flows", [])
print(f"flows type: {type(flows).__name__}, count: {len(flows)}")
if isinstance(flows, dict):
    for i, (k, f) in enumerate(list(flows.items())[:5]):
        print(f"  flow[{k}]: name={f.get('name','') if isinstance(f,dict) else f}")
        steps = f.get("steps", []) if isinstance(f, dict) else []
        print(f"    steps: {len(steps)}")
elif isinstance(flows, list):
    for i, f in enumerate(flows[:5]):
        print(f"  flow[{i}]: id={f.get('id','')}, name={f.get('name','')}")
        steps = f.get("steps", [])
        print(f"    steps: {len(steps)}")

# test_points_hint
tph = srr.get("test_points_hint", [])
print(f"\ntest_points_hint count: {len(tph)}")
print(f"test_points_hint sample: {json.dumps(tph[:2], ensure_ascii=False)[:300]}")

# Other fields
print(f"\nbusiness_rules: {len(srr.get('business_rules', []))}")
print(f"data_constraints: {len(srr.get('data_constraints', []))}")
print(f"boundary_conditions: {len(srr.get('boundary_conditions', []))}")
print(f"preconditions: {len(srr.get('preconditions', []))}")
print(f"acceptance_criteria: {len(srr.get('acceptance_criteria', []))}")

# impact_analysis
ia = srr.get("impact_analysis", [])
print(f"\nimpact_analysis: {len(ia) if isinstance(ia, list) else 'not list'}")
if ia:
    print(f"  sample: {json.dumps(ia[:1], ensure_ascii=False)[:200]}")

# requirement section
req_sec = srr.get("requirement", {})
print(f"\nrequirement.id: {req_sec.get('id')}")
print(f"requirement.title: {req_sec.get('title')}")
print(f"requirement.modules: {req_sec.get('modules')}")

# FP list - 正确路径是 /fp 不是 /functional-points
r2 = requests.get(f"{BASE}/requirements/REQ-1784205007538-5997/fp", headers=h)
fp_resp = r2.json()
print(f"\n=== FP API 响应 (/{'fp'}) ===")
print(f"status: {r2.status_code}")
print(f"success: {fp_resp.get('success')}")
fp_data = fp_resp.get("data", [])
if isinstance(fp_data, dict):
    fps = fp_data.get("functional_points", [])
    print(f"data keys: {list(fp_data.keys())}")
else:
    fps = fp_data
print(f"FP count: {len(fps)}")
for fp in fps:
    print(f"  fp: id={fp.get('id')}, name={fp.get('name')}, type={fp.get('fp_type')}, module={fp.get('module_id')}")

# Check if there's a test_points_hint with FP-like data
print(f"\n=== 检查 test_points_hint 内容 ===")
for i, tp in enumerate(tph[:3]):
    print(f"  tp[{i}]: {json.dumps(tp, ensure_ascii=False)[:200]}")

# Check raw_text
raw = d.get("raw_text", "")
print(f"\nraw_text length: {len(raw)}")
print(f"raw_text preview: {raw[:200]}")
