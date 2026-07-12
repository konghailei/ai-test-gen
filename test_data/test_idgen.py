"""Quick test: verify new ID generation is working."""
import requests
import json

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Upload a minimal test
payload = {
    "raw_text": "# 测试需求\n\n系统需提供一个简单的用户注册功能。",
    "product_id": "PROD-TEST-IDGEN",
    "product_name": "ID测试",
    "title": "ID生成测试",
    "priority": "P3",
    "modules": ["auth"],
}

r = requests.post(f"{BASE}/requirements/upload", json=payload, headers=h, timeout=120)
print(f"Status: {r.status_code}")
resp = r.json()
if resp.get("success"):
    req_id = resp["data"]["req_id"]
    print(f"Generated req_id: {req_id}")
    if req_id.startswith("REQ-2024-001"):
        print("WARNING: Still using hardcoded ID! Code changes not loaded.")
    else:
        print("OK: New unique ID generated.")
    # Clean up this test
    r = requests.delete(f"{BASE}/requirements/{req_id}", headers=h, timeout=30)
    print(f"Cleanup delete: {r.status_code}")
else:
    print(f"Upload failed: {resp.get('message', '')}")
