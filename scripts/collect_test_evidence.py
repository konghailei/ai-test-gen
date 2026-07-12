"""收集测试证据用于生成测试文档。"""
import json
import requests

API_BASE = "http://localhost:8000"


def login() -> str:
    resp = requests.post(
        f"{API_BASE}/api/auth/login",
        json={"username": "admin", "password": "888888"},
        timeout=15,
    )
    return resp.json()["data"]["token"]


def main():
    token = login()
    headers = {"Authorization": f"Bearer {token}"}
    evidence = {}

    # 1. 健康检查
    r = requests.get(f"{API_BASE}/api/health", headers=headers, timeout=15)
    evidence["health"] = r.json().get("data", {})

    # 2. 需求列表
    r = requests.get(f"{API_BASE}/api/requirements?limit=50", headers=headers, timeout=15)
    data = r.json().get("data", {})
    evidence["requirements"] = {
        "total": data.get("total", 0),
        "items": [
            {
                "id": it.get("id"),
                "title": it.get("title"),
                "status": it.get("status"),
                "product_id": it.get("product_id"),
                "priority": it.get("priority"),
                "updated_at": it.get("updated_at"),
            }
            for it in data.get("items", [])
        ],
    }

    # 3. 测试用例
    r = requests.get(f"{API_BASE}/api/testcases?limit=50", headers=headers, timeout=15)
    data = r.json().get("data", {})
    cases = data.get("items", [])
    evidence["test_cases"] = {
        "total": data.get("total", 0),
        "items": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "req_id": c.get("req_id"),
                "priority": c.get("priority"),
                "type": c.get("type"),
                "confidence": c.get("confidence"),
                "status": c.get("status"),
            }
            for c in cases
        ],
    }

    # 4. 产品
    r = requests.get(f"{API_BASE}/api/products/products", headers=headers, timeout=15)
    evidence["products"] = r.json().get("data", [])

    # 5. 知识库统计
    r = requests.get(f"{API_BASE}/api/knowledge/stats", headers=headers, timeout=15)
    evidence["kb_stats"] = r.json().get("data", {})

    # 6. 知识库条目
    r = requests.get(f"{API_BASE}/api/knowledge/entries?limit=20", headers=headers, timeout=15)
    data = r.json().get("data", {})
    evidence["kb_entries"] = {
        "total": data.get("total", 0),
        "items": [
            {
                "id": it.get("id"),
                "title": it.get("title"),
                "type": it.get("type"),
                "status": it.get("status"),
                "vectorized": it.get("vectorized"),
                "go_live_date": it.get("go_live_date"),
                "project_version": it.get("project_version"),
            }
            for it in data.get("items", [])
        ],
    }

    # 7. LLM 设置
    r = requests.get(f"{API_BASE}/api/settings/llm", headers=headers, timeout=15)
    data = r.json().get("data", {})
    evidence["llm_settings"] = {
        "providers": [
            {
                "provider": p.get("provider"),
                "model": p.get("model"),
                "enabled": p.get("enabled"),
                "is_default": p.get("is_default"),
                "api_base": p.get("api_base"),
            }
            for p in data.get("providers", [])
        ],
        "builtin_providers_count": len(data.get("builtin_providers", [])),
    }

    # 8. 模式库
    r = requests.get(f"{API_BASE}/api/knowledge/patterns", headers=headers, timeout=15)
    data = r.json().get("data", {})
    patterns = data.get("items", [])
    evidence["patterns"] = {
        "total": len(patterns),
        "items": [
            {"name": p.get("name"), "category": p.get("category")}
            for p in patterns
        ],
    }

    # 9. 测试用例生成事件（从已存的日志读取）
    try:
        with open("e:/git-down/ai-test-gen/logs/generate_REQ-1783838540411-9076.json", "r", encoding="utf-8") as f:
            events = json.load(f)
        evidence["generate_events"] = [
            {
                "stage": e.get("stage"),
                "progress": e.get("progress"),
                "message": e.get("message"),
            }
            for e in events
        ]
        done_evts = [e for e in events if e.get("stage") == "done"]
        if done_evts:
            d = done_evts[-1]
            evidence["generate_done_summary"] = {
                "req_id": d.get("req_id"),
                "channel": d.get("channel"),
                "reviewed_cases_count": len(d.get("reviewed_cases", [])),
                "review_report": d.get("review_report"),
            }
    except Exception as e:
        evidence["generate_events_error"] = str(e)

    # 输出
    out_path = "e:/git-down/ai-test-gen/logs/test_evidence.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(evidence, f, ensure_ascii=False, indent=2, default=str)
    print(f"[OK] 证据已写入 {out_path}")
    print(f"  - 需求: {evidence['requirements']['total']} 条")
    print(f"  - 测试用例: {evidence['test_cases']['total']} 条")
    print(f"  - 产品: {len(evidence['products'])} 个")
    print(f"  - 知识库条目: {evidence['kb_entries']['total']} 条")
    print(f"  - 模式库: {evidence['patterns']['total']} 个")
    print(f"  - 生成事件: {len(evidence.get('generate_events', []))} 个")


if __name__ == "__main__":
    main()
