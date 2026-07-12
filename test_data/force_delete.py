"""Force delete archived requirement by providing required srr_json."""
import requests
import json

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Get the archived requirement details
r = requests.get(f"{BASE}/requirements/REQ-2024-001", headers=h, timeout=30)
data = r.json().get("data", {})
srr_json = data.get("srr_json", {})
print(f"Got SRR with {len(srr_json)} keys")

# Update status to active (need to provide required fields)
update_body = {
    "srr_json": srr_json,
    "status": "active",
}
r = requests.put(f"{BASE}/requirements/REQ-2024-001", json=update_body, headers=h, timeout=30)
print(f"Update status: {r.status_code}")
if r.status_code == 200:
    print("  Status changed to active")
    # Now delete
    r = requests.delete(f"{BASE}/requirements/REQ-2024-001", headers=h, timeout=30)
    print(f"Delete: {r.status_code} {r.json().get('message', '')}")
else:
    print(f"  Failed: {r.text[:300]}")

# Check remaining
r = requests.get(f"{BASE}/requirements?limit=100", headers=h, timeout=30)
data = r.json().get("data", {})
items = data.get("items", []) if isinstance(data, dict) else data
print(f"\nRemaining requirements: {len(items)}")

# Check health
r = requests.get(f"{BASE}/health", timeout=10)
vs = r.json().get("data", {}).get("vector_db", {})
print(f"Vector DB collections: {vs.get('collections', {})}")
