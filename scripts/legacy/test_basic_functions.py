"""E2E 测试：验证系统基础功能。"""
import json
import sys

import requests

API_BASE = "http://localhost:8000"
results = []


def login() -> str:
    resp = requests.post(
        f"{API_BASE}/api/auth/login",
        json={"username": "admin", "password": "888888"},
        timeout=15,
    )
    return resp.json()["data"]["token"]


def api_get(token, path):
    resp = requests.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    ct = resp.headers.get("content-type", "")
    return resp.status_code, resp.json() if ct.startswith("application/json") else resp.text


def api_post(token, path, body=None):
    resp = requests.post(f"{API_BASE}{path}", json=body or {},
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=30)
    ct = resp.headers.get("content-type", "")
    return resp.status_code, resp.json() if ct.startswith("application/json") else resp.text


def api_delete(token, path):
    resp = requests.delete(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    ct = resp.headers.get("content-type", "")
    return resp.status_code, resp.json() if ct.startswith("application/json") else resp.text


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))


def main():
    print("=== 基础功能验证 ===\n")
    token = login()
    print(f"[OK] 登录成功\n")

    # 1. 需求列表
    print("1. 需求列表")
    code, data = api_get(token, "/api/requirements?limit=5")
    check("GET /api/requirements", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        items = data.get("data", {}).get("items", [])
        total = data.get("data", {}).get("total", 0)
        check(f"返回 {len(items)} 条需求（总计 {total}）", len(items) > 0, f"items={len(items)}")

    # 2. 需求详情
    print("\n2. 需求详情")
    req_id = "REQ-1783838540411-9076"
    code, data = api_get(token, f"/api/requirements/{req_id}")
    check(f"GET /api/requirements/{req_id}", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        d = data.get("data", {})
        check("状态为 archived", d.get("status") == "archived", f"status={d.get('status')}")
        check("含 srr_json", bool(d.get("srr_json")), f"srr_json={'有' if d.get('srr_json') else '无'}")

    # 3. 测试用例列表
    print("\n3. 测试用例列表")
    code, data = api_get(token, "/api/testcases?limit=10")
    check("GET /api/testcases", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        items = data.get("data", {}).get("items", [])
        check(f"返回 {len(items)} 条用例", len(items) > 0, f"items={len(items)}")
        if items:
            check("用例 req_id 关联正确", items[0].get("req_id") == req_id, f"req_id={items[0].get('req_id')}")

    # 4. 产品管理
    print("\n4. 产品管理")
    code, data = api_get(token, "/api/products/products")
    check("GET /api/products/products", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        products = data.get("data", [])
        if not isinstance(products, list):
            products = products.get("items", [])
        check(f"返回 {len(products)} 个产品", len(products) >= 0)

    code, data = api_post(token, "/api/products/products", {"name": "E2E测试产品", "description": "自动化测试"})
    check("POST /api/products/products (创建)", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        prod_data = data.get("data", {})
        prod_id = prod_data.get("id", "") if isinstance(prod_data, dict) else ""
        check(f"产品ID: {prod_id}", bool(prod_id))
        if prod_id:
            code, _ = api_delete(token, f"/api/products/products/{prod_id}")
            check("DELETE /api/products/products/{id}", code == 200, f"code={code}")

    # 5. 模块管理（用 PROD-DEFAULT）
    print("\n5. 模块管理")
    code, data = api_get(token, "/api/products/products/PROD-DEFAULT/modules")
    check("GET /api/products/products/PROD-DEFAULT/modules", code == 200 and data.get("success"), f"code={code}")

    # 6. 依赖图
    print("\n6. 依赖图")
    code, data = api_get(token, "/api/products/dependency-graph")
    check("GET /api/products/dependency-graph", code == 200 and data.get("success"), f"code={code}")

    # 7. 知识库统计
    print("\n7. 知识库统计")
    code, data = api_get(token, "/api/knowledge/stats")
    check("GET /api/knowledge/stats", code == 200 and data.get("success"), f"code={code}")

    # 8. 知识库条目列表
    print("\n8. 知识库条目")
    code, data = api_get(token, "/api/knowledge/entries?limit=5")
    check("GET /api/knowledge/entries", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        items = data.get("data", {}).get("items", [])
        total = data.get("data", {}).get("total", 0)
        check(f"知识库条目 {total} 条", total > 0, f"total={total}")

    # 9. 知识库问答（SSE 流式）
    print("\n9. 知识库问答")
    try:
        qa_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"}
        with requests.post(
            f"{API_BASE}/api/knowledge-qa/ask",
            json={"question": "用户注册功能有哪些边界条件？", "product_id": "PROD-DEFAULT"},
            headers=qa_headers, stream=True, timeout=120,
        ) as qa_resp:
            qa_ok = qa_resp.status_code == 200
            answer_parts = []
            current_event = ""
            for line in qa_resp.iter_lines(decode_unicode=True):
                if not line:
                    current_event = ""
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    raw_data = line[5:].strip()
                    try:
                        parsed = json.loads(raw_data)
                    except json.JSONDecodeError:
                        parsed = raw_data
                    if current_event == "chunk":
                        if isinstance(parsed, str):
                            answer_parts.append(parsed)
                        elif isinstance(parsed, dict):
                            answer_parts.append(parsed.get("data", parsed.get("content", "")))
                    elif current_event == "done":
                        if isinstance(parsed, dict):
                            answer_parts.append(parsed.get("answer", ""))
            answer = "".join(str(p) for p in answer_parts)
            check("POST /api/knowledge-qa/ask (SSE)", qa_ok, f"status={qa_resp.status_code}")
            check(f"返回答案长度: {len(answer)}", len(answer) > 0, f"len={len(answer)}")
            if answer:
                print(f"  答案前200字: {answer[:200]}...")
    except Exception as e:
        check("POST /api/knowledge-qa/ask (SSE)", False, f"异常: {str(e)[:150]}")

    # 10. LLM 设置
    print("\n10. LLM 设置")
    code, data = api_get(token, "/api/settings/llm")
    check("GET /api/settings/llm", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        providers = data.get("data", {}).get("providers", [])
        check(f"返回 {len(providers)} 个供应商配置", len(providers) > 0, f"providers={len(providers)}")
        if providers:
            p = providers[0]
            check(f"主供应商: {p.get('provider', '?')}", bool(p.get("provider")), f"provider={p.get('provider')}")
            check(f"模型: {p.get('model', '?')}", bool(p.get("model")), f"model={p.get('model')}")

    # 11. 测试模式库
    print("\n11. 测试模式库")
    code, data = api_get(token, "/api/knowledge/patterns")
    check("GET /api/knowledge/patterns", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        patterns = data.get("data", {}).get("items", [])
        check(f"返回 {len(patterns)} 个测试模式", len(patterns) > 0, f"patterns={len(patterns)}")

    # 12. 系统健康
    print("\n12. 系统健康")
    code, data = api_get(token, "/api/health")
    check("GET /api/health", code == 200 and data.get("success"), f"code={code}")
    if data.get("success"):
        d = data.get("data", {})
        check(f"LLM provider: {d.get('llm_provider')}", bool(d.get("llm_provider")))
        check(f"向量库已连接", d.get("vector_db", {}).get("connected") is True)
        check(f"KB 非空", d.get("kb_empty") is False)

    # 汇总
    print("\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"总计: {total} 项 | 通过: {passed} | 失败: {failed}")
    if failed:
        print("\n失败项:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}: {detail}")
    print("=" * 60)

    out_path = "e:/git-down/ai-test-gen/logs/basic_functions_test.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([{"name": n, "passed": ok, "detail": d} for n, ok, d in results], f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {out_path}")


if __name__ == "__main__":
    main()
