# -*- coding: utf-8 -*-
"""环境检查：登录 + 服务就绪 + 现有测试数据水位 + 新端点存在性。"""
import json, sys, time
import requests as _rq

BASE = "http://localhost/api"
OUT = []

TOKEN = None

def req(method, path, body=None, timeout=60):
    url = BASE + path
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    try:
        if body is not None:
            r = _rq.request(method, url, json=body, headers=headers, timeout=timeout)
        else:
            r = _rq.request(method, url, headers=headers, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_raw": r.text[:500]}
    except Exception as e:
        return -1, {"_err": str(e)[:300]}

def dump(name, obj):
    p = r"artifacts/{}".format(name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    print("[artifact] wrote", name)

# 1. login
st, resp = req("POST", "/auth/login", {"username": "admin", "password": "888888"})
print("login", st, str(resp)[:120])
if st == 200 and resp.get("success"):
    TOKEN = resp["data"]["token"]
    dump("env_admin_token.json", {"token": TOKEN})
else:
    print("FATAL login failed"); sys.exit(1)

# 2. products list
st, resp = req("GET", "/products/")
print("products", st)
if st == 200:
    data = resp.get("data", [])
    if isinstance(data, dict):
        data = data.get("items", data.get("list", [])) or []
    recs = []
    for p in data:
        recs.append({"id": p.get("id"), "name": p.get("name"), "status": p.get("status"),
                     "created": p.get("created_at"), "desc": (p.get("description") or "")[:30]})
    OUT.append({"products_count": len(recs), "products": recs})
    print(json.dumps(recs, ensure_ascii=False, indent=1))
    dump("env_products.json", recs)
else:
    print("products resp", str(resp)[:300])

# 3. knowledge stats
st, resp = req("GET", "/knowledge/stats", timeout=90)
print("knowledge/stats", st, str(resp.get("data", {}))[:200] if isinstance(resp, dict) else "")
OUT.append({"knowledge_stats": resp.get("data", {}) if isinstance(resp, dict) else {}})

# 4. new endpoint probes (fp-crsr)
st, resp = req("GET", "/knowledge/stats", timeout=30)
print("probe /knowledge/stats ok", st)

# 5. cluster health via admin
st, resp = req("GET", "/admin/users", timeout=30)
print("admin/users", st, "count=", len(resp.get("data", [])) if st == 200 else resp.get("detail"))

dump("env_check_summary.json", OUT)
print("\nDONE")