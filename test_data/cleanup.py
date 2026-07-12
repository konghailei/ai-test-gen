"""Clean up old test data."""
import requests

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}"}

# Delete old test data
for rid in ["REQ-2024-001", "REQ-2024-001-V2.0"]:
    r = requests.delete(f"{BASE}/requirements/{rid}", headers=h, timeout=30)
    msg = r.json().get("message", "")
    print(f"Delete {rid}: {r.status_code} {msg}")

# Verify
r = requests.get(f"{BASE}/requirements?limit=50", headers=h, timeout=30)
data = r.json().get("data", {})
items = data.get("items", []) if isinstance(data, dict) else data
print(f"Remaining requirements: {len(items)}")

# Check health
r = requests.get(f"{BASE}/health", timeout=10)
vs = r.json().get("data", {}).get("vector_db", {})
collections = vs.get("collections", {})
print(f"Vector DB collections: {collections}")
