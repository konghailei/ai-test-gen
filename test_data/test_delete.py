import requests

login_resp = requests.post('http://localhost:8000/api/auth/login', json={'username': 'admin', 'password': '888888'})
token = login_resp.json()['data']['token']
headers = {'Authorization': f'Bearer {token}'}

# List KB entries to find one to try deleting
resp = requests.get('http://localhost:8000/api/knowledge/entries?limit=5', headers=headers)
entries = resp.json()['data']['items']
print(f"Found {len(entries)} KB entries")
for e in entries[:3]:
    print(f"  id={e['id']} type={e['type']} title={e.get('title', '')[:40]}")

# Try deleting the first requirement entry
req_entries = [e for e in entries if e['type'] == 'requirement']
if req_entries:
    entry = req_entries[0]
    print(f"\nTrying to delete: id={entry['id']} type={entry['type']}")
    del_resp = requests.delete(
        f'http://localhost:8000/api/knowledge/entries/{entry["id"]}?entry_type={entry["type"]}',
        headers=headers
    )
    print(f"Status: {del_resp.status_code}")
    print(f"Response: {del_resp.json()}")
else:
    print("No requirement entries found to test delete")

# Also check backend logs for the error
print("\n--- Checking backend logs ---")
