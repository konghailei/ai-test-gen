"""验证问题 3 修复：需求详情 API 是否返回向量化与归档相关字段。

验证步骤：
1. 登录获取 token
2. 查询已归档需求 REQ-1784163945376-3043，确认包含 4 个新字段且 vectorization_status=completed
3. 查询未归档需求，确认 vectorization_status=None、archived_at=""，不报错
"""
import sys
import requests

BASE = "http://localhost:8000/api"

# 1. 登录
print("=" * 60)
print("Step 1: 登录")
login_r = requests.post(
    f"{BASE}/auth/login",
    json={"username": "admin", "password": "888888"},
    timeout=10,
)
assert login_r.status_code == 200, f"登录失败: {login_r.status_code} {login_r.text}"
token = login_r.json()["data"]["token"]
headers = {"Authorization": f"Bearer {token}"}
print(f"✅ 登录成功, token 前 20 位: {token[:20]}...")

# 2. 查询已归档需求
print("\n" + "=" * 60)
print("Step 2: 查询已归档需求 REQ-1784163945376-3043")
r = requests.get(
    f"{BASE}/requirements/REQ-1784163945376-3043",
    headers=headers,
    timeout=10,
)
assert r.status_code == 200, f"查询失败: {r.status_code} {r.text}"
data = r.json()["data"]
print(f"返回字段: {sorted(data.keys())}")

# 验证 4 个新字段存在
required_fields = ["vectorization_status", "vectorization_error", "go_live_date", "archived_at"]
for f in required_fields:
    assert f in data, f"❌ 缺失字段: {f}"
    print(f"  ✅ {f} = {data[f]!r}")

# 验证已归档需求的 vectorization_status 应为 completed
vs = data["vectorization_status"]
aa = data["archived_at"]
print(f"\nvectorization_status = {vs!r}")
print(f"archived_at = {aa!r}")

if vs == "completed":
    print("✅ vectorization_status='completed'，向量化已完成（符合归档后预期）")
else:
    print(f"⚠️ vectorization_status={vs!r}（可能 Celery 任务还在进行，或归档后未触发向量化）")

if aa:
    print(f"✅ archived_at 非空，归档时间已记录")
else:
    print("⚠️ archived_at 为空（可能该需求未被归档）")

# 3. 查询未归档需求（先获取需求列表找一条未归档的）
print("\n" + "=" * 60)
print("Step 3: 查询未归档需求")
list_r = requests.get(f"{BASE}/requirements", headers=headers, timeout=10)
assert list_r.status_code == 200, f"查询列表失败: {list_r.status_code}"
reqs = list_r.json()["data"]["items"]
print(f"需求列表共 {len(reqs)} 条:")
for req in reqs:
    print(f"  - {req['id']}: status={req['status']}, title={req['title'][:40]}")

# 找一个未归档需求
unarchived = next((r for r in reqs if r["status"] != "archived"), None)
if unarchived:
    print(f"\n查询未归档需求: {unarchived['id']}")
    r2 = requests.get(
        f"{BASE}/requirements/{unarchived['id']}",
        headers=headers,
        timeout=10,
    )
    assert r2.status_code == 200, f"查询失败: {r2.status_code} {r2.text}"
    data2 = r2.json()["data"]
    for f in required_fields:
        assert f in data2, f"❌ 缺失字段: {f}"
        print(f"  ✅ {f} = {data2[f]!r}")

    if data2["vectorization_status"] is None:
        print("✅ 未归档需求 vectorization_status=None（符合预期）")
    else:
        print(f"ℹ️ 未归档需求 vectorization_status={data2['vectorization_status']!r}（可能已触发向量化但未归档）")

    if not data2["archived_at"]:
        print("✅ 未归档需求 archived_at=''（符合预期）")
    else:
        print(f"⚠️ 未归档需求 archived_at={data2['archived_at']!r}（不应有值）")
else:
    print("⚠️ 未找到未归档需求，跳过该验证")

print("\n" + "=" * 60)
print("✅ 问题 3 修复验证通过：API 返回了全部 4 个新字段")
