"""Quick test: verify the Hit.get() fix is working by doing a knowledge QA search."""
import requests
import json

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}"}

# Check health first
r = requests.get(f"{BASE}/health", timeout=10)
vs = r.json().get("data", {}).get("vector_db", {})
print(f"Vector DB connected: {vs.get('connected')}")
print(f"Requirements in DB: {vs.get('collections', {}).get('requirements', 'N/A')}")

# Try knowledge QA
question = "登录功能的正常流程是什么？"
print(f"\n[Q] {question}")
r = requests.post(f"{BASE}/knowledge-qa/ask", json={
    "question": question,
    "product_id": "PROD-TEST-E2E",
}, headers=h, timeout=120, stream=True)

sources = []
answer = ""
kb_status = None
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
        elif event_type == "done":
            done_data = json.loads(payload)
            answer = done_data.get("answer_full", "")
            kb_status = done_data.get("kb_status")
        elif event_type == "chunk":
            answer = payload

print(f"\nSources found: {len(sources)}")
for s in sources:
    print(f"  - {s.get('title', 'N/A')} (similarity: {s.get('similarity', 0)})")
print(f"\nKB Status: {kb_status}")
print(f"\nAnswer (first 500 chars):\n{answer[:500]}")
