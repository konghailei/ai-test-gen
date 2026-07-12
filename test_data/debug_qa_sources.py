"""Debug: check full source data from QA search."""
import requests
import json

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}"}

# QA search
question = "登录功能的正常流程是什么？"
r = requests.post(f"{BASE}/knowledge-qa/ask", json={
    "question": question,
    "product_id": "PROD-TEST-E2E",
}, headers=h, timeout=120, stream=True)

event_type = None
for line in r.iter_lines(decode_unicode=True):
    if not line:
        continue
    if line.startswith("event: "):
        event_type = line[7:].strip()
    elif line.startswith("data: "):
        payload = line[6:]
        if event_type == "sources":
            sources = json.loads(payload)
            print(f"\n=== SOURCES ({len(sources)} items) ===")
            for s in sources:
                print(json.dumps(s, indent=2, ensure_ascii=False))
        elif event_type == "done":
            done_data = json.loads(payload)
            print(f"\n=== DONE ===")
            print(f"kb_status: {done_data.get('kb_status')}")
            ref_sources = done_data.get("reference_sources", [])
            print(f"\nreference_sources ({len(ref_sources)} items):")
            for s in ref_sources:
                print(json.dumps(s, indent=2, ensure_ascii=False))
