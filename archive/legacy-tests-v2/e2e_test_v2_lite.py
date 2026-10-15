"""轻量级验证脚本 — 直接验证新增功能（历史版本、XMind 导出、项目/模块管理）。

策略：
1. 列出已有需求，找到至少一个 active/confirmed 状态的需求用于历史版本测试
2. 找到一个已归档需求用于 XMind 导出 + KB 检索验证
3. 验证项目管理跳转参数（project_id 过滤）
"""
import sys, time, io, zipfile, json
import requests

BASE = "http://localhost:8000"
TOKEN = None

def login():
    global TOKEN
    r = requests.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "888888"}, timeout=15)
    data = r.json()["data"]
    TOKEN = data["token"]
    print(f"[OK] 登录成功: {data['user']['username']}")

def h():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def get(path):
    r = requests.get(f"{BASE}{path}", headers=h(), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
    d = r.json()
    return d.get("data", d) if isinstance(d, dict) and "success" in d else d

def post(path, body=None):
    r = requests.post(f"{BASE}{path}", json=body, headers=h(), timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
    d = r.json()
    return d.get("data", d) if isinstance(d, dict) and "success" in d else d

def get_raw(path):
    return requests.get(f"{BASE}{path}", headers=h(), timeout=120)

results = []
def check(name, ok, detail=""):
    status = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
    print(f"  [{status}] {name}  {detail if not ok else ''}")
    results.append((name, ok))

def section(t):
    print(f"\n=== {t} ===")

# ============================================================================

section("0. 登录")
login()

section("1. 项目/模块管理 - 列表接口")
products = get("/api/products/products")
check("产品列表", isinstance(products, list) and len(products) > 0, f"type={type(products).__name__}")

# 取第一个产品
pid = products[0]["id"]
print(f"  使用产品: {pid} ({products[0].get('name')})")

# 项目列表（完整路径 /api/products/products/{pid}/projects）
projs = get(f"/api/products/products/{pid}/projects")
check("项目列表", isinstance(projs, list), f"数量={len(projs)}")

# 模块列表
mods = get(f"/api/products/products/{pid}/modules")
check("模块列表", isinstance(mods, list), f"数量={len(mods)}")

# 依赖图谱
try:
    g = get(f"/api/products/dependency-graph?product_id={pid}")
    check("依赖图谱查询", isinstance(g, dict) and "nodes" in g, f"keys={list(g.keys())[:5] if isinstance(g, dict) else 'N/A'}")
except Exception as e:
    check("依赖图谱查询", False, str(e)[:150])

section("2. 需求列表 + 项目过滤")
# 全部需求
all_reqs = get("/api/requirements?page=1&page_size=50")
items = all_reqs.get("items", []) if isinstance(all_reqs, dict) else []
check("需求列表", isinstance(items, list), f"total={all_reqs.get('total') if isinstance(all_reqs, dict) else 'N/A'}")

# 找一个 active/confirmed 的需求（用于历史版本测试）
active_req = next((r for r in items if r.get("status") in ("active", "confirmed")), None)
archived_req = next((r for r in items if r.get("status") == "archived"), None)
print(f"  找到 active/confirmed 需求: {active_req['id'] if active_req else '无'}")
print(f"  找到 archived 需求: {archived_req['id'] if archived_req else '无'}")

# 按项目过滤（如果有项目）
if projs:
    proj_id = projs[0]["id"]
    try:
        r = get(f"/api/requirements?page=1&page_size=10&project_id={proj_id}")
        check(f"按 project_id 过滤需求 (proj={proj_id})", isinstance(r, dict), f"total={r.get('total')}")
    except Exception as e:
        check("按 project_id 过滤需求", False, str(e)[:150])

    try:
        r = get(f"/api/testcases?page=1&page_size=10&project_id={proj_id}")
        check("按 project_id 过滤用例", isinstance(r, dict), f"total={r.get('total')}")
    except Exception as e:
        check("按 project_id 过滤用例", False, str(e)[:150])

section("3. XMind 导出（v2.0 新功能）")
# 用任意一个需求测试 XMind 导出
test_req_id = (archived_req or active_req or (items[0] if items else None))
if test_req_id:
    rid = test_req_id["id"]
    try:
        resp = get_raw(f"/api/requirements/{rid}/export/xmind")
        check("XMind HTTP 200", resp.status_code == 200, f"status={resp.status_code}")

        if resp.status_code == 200:
            content = resp.content
            check("返回内容非空", len(content) > 100, f"size={len(content)} bytes")

            # 验证是合法的 ZIP
            buf = io.BytesIO(content)
            with zipfile.ZipFile(buf, "r") as zf:
                names = zf.namelist()
                check("ZIP 包含 content.xml", "content.xml" in names, f"files={names}")

                if "content.xml" in names:
                    xml = zf.read("content.xml").decode("utf-8", errors="ignore")
                    check("XML 内容有效", "<xmap-content" in xml and "<topic" in xml, f"xml_len={len(xml)}")

                    # 解析 XML，统计 topic 数量
                    topic_count = xml.count("<topic ")
                    print(f"  XML 包含 {topic_count} 个 topic 节点")

            # 验证 Content-Disposition（含中文文件名 RFC 5987 编码）
            cd = resp.headers.get("Content-Disposition", "")
            check("Content-Disposition 含 filename*", "filename*=UTF-8" in cd, cd[:200])
    except Exception as e:
        check("XMind 导出", False, str(e)[:300])

section("4. 历史版本管理 - 查询接口")
# 用 active 需求测试历史版本查询（如果有的话）
if active_req:
    rid = active_req["id"]
    try:
        # 查询历史版本列表（即使没有历史版本也应该返回空列表）
        v = get(f"/api/requirements/{rid}/versions")
        v_items = v.get("items", []) if isinstance(v, dict) else []
        check("历史版本列表接口", isinstance(v_items, list), f"版本数={len(v_items)}")

        if v_items:
            # 查看首个版本详情
            v_idx = v_items[0].get("version_index", 1)
            v_detail = get(f"/api/requirements/{rid}/versions/{v_idx}")
            has_srr = isinstance(v_detail, dict) and "srr" in v_detail
            check("历史版本详情接口", has_srr, f"version_index={v_idx}, has_srr={has_srr}")
        else:
            check("历史版本详情接口", True, "(无历史版本，跳过详情查询)")
    except Exception as e:
        check("历史版本查询", False, str(e)[:300])
else:
    check("历史版本管理", False, "无 active/confirmed 需求可用于测试")

section("5. 历史版本管理 - 触发重新生成（创建历史版本）")
if active_req:
    rid = active_req["id"]
    try:
        # 触发重新生成
        result = post(f"/api/requirements/{rid}/regenerate", {"extra_notes": "E2E 测试：补充历史版本验证"})
        task_id = result.get("task_id")
        check("触发重新生成", bool(task_id), f"task_id={task_id}")

        # 轮询任务状态（最多 300s，LLM 调用较慢）
        final_status = None
        for _ in range(150):
            try:
                task = get(f"/api/requirements/tasks/{task_id}")
                final_status = task.get("status")
                if final_status in ("completed", "failed", "interrupted"):
                    break
            except Exception:
                pass
            time.sleep(2)
        check("重新生成任务完成", final_status == "completed", f"final_status={final_status}")

        # 重新查询历史版本
        if final_status == "completed":
            v = get(f"/api/requirements/{rid}/versions")
            v_items = v.get("items", []) if isinstance(v, dict) else []
            check("历史版本已创建", len(v_items) > 0, f"版本数={len(v_items)}")

            if v_items:
                # 验证版本详情
                v_idx = v_items[0].get("version_index", 1)
                v_detail = get(f"/api/requirements/{rid}/versions/{v_idx}")
                has_srr = isinstance(v_detail, dict) and "srr" in v_detail
                check("历史版本详情含 SRR", has_srr, f"version_index={v_idx}")

                # 验证 SRR 内容（应有 requirement/flows 等字段）
                if has_srr:
                    srr = v_detail["srr"]
                    srr_keys = list(srr.keys()) if isinstance(srr, dict) else []
                    print(f"  历史 SRR 字段: {srr_keys[:8]}")
                    check("历史 SRR 含 requirement 字段", "requirement" in srr_keys, f"keys={srr_keys[:8]}")
    except Exception as e:
        check("历史版本触发", False, str(e)[:300])

section("6. 用例 XMind 导出（已有功能回归）")
# 找一个有用例的需求
if active_req or archived_req:
    rid = (active_req or archived_req)["id"]
    try:
        # 查询该需求的用例
        cases = get(f"/api/testcases?req_id={rid}&page=1&page_size=5")
        case_items = cases.get("items", []) if isinstance(cases, dict) else []
        if case_items:
            case_ids = [c["id"] for c in case_items[:3]]
            resp = requests.post(f"{BASE}/api/export", json={"format": "xmind", "case_ids": case_ids}, headers=h(), timeout=60)
            check("用例 XMind HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
            if resp.status_code == 200:
                content = resp.content
                buf = io.BytesIO(content)
                with zipfile.ZipFile(buf, "r") as zf:
                    names = zf.namelist()
                    check("用例 ZIP 含 content.xml", "content.xml" in names, f"files={names}")
        else:
            check("用例 XMind 导出", True, "(该需求无用例，跳过)")
    except Exception as e:
        check("用例 XMind 导出", False, str(e)[:300])

# ============================================================================
# 汇总
# ============================================================================
section("测试汇总")
total = len(results)
passed = sum(1 for _, ok in results if ok)
failed = total - passed
print(f"  总计: {total}  通过: {passed}  失败: {failed}")
if failed:
    print("\n失败项:")
    for name, ok in results:
        if not ok:
            print(f"  - {name}")
    sys.exit(1)
else:
    print("\n全部测试通过！")
