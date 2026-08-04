"""Clean up ALL test data — delete all requirements and clean Milvus."""
import requests
import json

BASE = "http://localhost:8000/api"
r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
token = r.json()["data"]["token"]
h = {"Authorization": f"Bearer {token}"}

# List all requirements
r = requests.get(f"{BASE}/requirements?limit=100", headers=h, timeout=30)
data = r.json().get("data", {})
items = data.get("items", []) if isinstance(data, dict) else data
print(f"Found {len(items)} requirements to clean")

for item in items:
    rid = item.get("id", "")
    status = item.get("status", "")
    print(f"  Processing {rid} (status={status})...")

    # If archived, need to change status first
    if status == "archived":
        # Try to update status to active
        r = requests.put(f"{BASE}/requirements/{rid}", json={"status": "active"}, headers=h, timeout=30)
        if r.status_code != 200:
            print(f"    Failed to update status: {r.status_code} {r.text[:200]}")
            continue
        print(f"    Changed to active")

    # Now delete
    r = requests.delete(f"{BASE}/requirements/{rid}", headers=h, timeout=30)
    msg = r.json().get("message", "")
    print(f"    Delete: {r.status_code} {msg}")

# Check remaining
r = requests.get(f"{BASE}/requirements?limit=100", headers=h, timeout=30)
data = r.json().get("data", {})
items = data.get("items", []) if isinstance(data, dict) else data
print(f"\nRemaining requirements: {len(items)}")

# Check vector DB
r = requests.get(f"{BASE}/health", timeout=10)
vs = r.json().get("data", {}).get("vector_db", {})
collections = vs.get("collections", {})
print(f"Vector DB collections: {collections}")
