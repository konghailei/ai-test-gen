"""v1.7.1 全流程测试：需求分析 → FP → 测试用例生成 → 质量验证。

前置条件：REQ-1784205007538-5997 已完成需求分析（status=active, 0 questions, 2 FPs in DB）。
"""
import json
import time
import sys
import os
import requests

BASE = "http://localhost:8000/api"
REQ_ID = "REQ-1784205007538-5997"
TIMEOUT = 30
SSE_TIMEOUT = 600  # 10 min

results = {"pass": 0, "fail": 0, "warn": 0, "items": []}


def record(name, status, detail=""):
    results["items"].append({"name": name, "status": status, "detail": detail})
    if status == "PASS":
        results["pass"] += 1
    elif status == "FAIL":
        results["fail"] += 1
    else:
        results["warn"] += 1
    print(f"  [{status}] {name}: {detail}")


def section(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


# ============ Step 1: 登录 ============
section("Step 1: 登录 + LLM 配置验证")
login = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=10)
assert login.status_code == 200, f"登录失败: {login.status_code}"
token = login.json()["data"]["token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
print("登录成功")

# LLM 配置 (正确端点: /api/settings/llm)
llm_resp = requests.get(f"{BASE}/settings/llm", headers=headers, timeout=10)
llm_data = llm_resp.json().get("data", {})
providers = llm_data.get("providers", [])
primary = next((p for p in providers if p.get("is_primary")), providers[0] if providers else {})
provider = primary.get("provider", "")
model = primary.get("model", "")
print(f"LLM 配置: provider={provider}, model={model}, providers_count={len(providers)}")
record("LLM 配置加载", "PASS" if provider and model else "FAIL", f"provider={provider}, model={model}")

# ============ Step 2: 需求分析结果验证 ============
section("Step 2: 需求分析结果（SRR）质量验证")
req_detail = requests.get(f"{BASE}/requirements/{REQ_ID}", headers=headers, timeout=TIMEOUT)
assert req_detail.status_code == 200, f"查询需求失败: {req_detail.status_code}"
rd = req_detail.json().get("data", {})

print(f"需求 ID: {rd.get('id')}")
print(f"标题: {rd.get('title')}")
print(f"状态: {rd.get('status')}")
print(f"优先级: {rd.get('priority')}")
print(f"模块: {rd.get('modules')}")
print(f"版本: {rd.get('version')}")
print(f"澄清问题数: {rd.get('questions_count')}")

record("需求状态为 active", "PASS" if rd.get("status") == "active" else "FAIL",
       f"status={rd.get('status')}")

# SRR 字段完整性 (v1.7.1 SRR 使用 flows 而非 baselines)
srr = rd.get("srr_json", {}) or {}
srr_keys = list(srr.keys())
print(f"\nSRR top-level keys ({len(srr_keys)}): {srr_keys}")

required_top_keys = ["requirement", "flows", "business_rules", "data_constraints", "boundary_conditions"]
missing_top = [k for k in required_top_keys if k not in srr]
record("SRR 必需顶层字段", "PASS" if not missing_top else "FAIL",
       f"missing={missing_top}" if missing_top else "requirement/flows/rules/constraints/boundary 齐全")

# requirement 子字段
req_section = srr.get("requirement", {})
print(f"\nrequirement.id: {req_section.get('id')}")
print(f"requirement.title: {req_section.get('title')}")
print(f"requirement.modules: {req_section.get('modules')}")
record("SRR.requirement 字段", "PASS" if req_section.get("id") and req_section.get("title") else "FAIL",
       f"id={req_section.get('id')}, title={req_section.get('title')}")

# flows 质量 (dict: happy_path + alternate_flows)
flows = srr.get("flows", {})
print(f"\nflows type: {type(flows).__name__}")
if isinstance(flows, dict):
    happy = flows.get("happy_path", {})
    happy_steps = happy.get("steps", []) if isinstance(happy, dict) else []
    alt_flows = flows.get("alternate_flows", []) if isinstance(flows.get("alternate_flows"), list) else []
    print(f"  happy_path steps: {len(happy_steps)}")
    print(f"  alternate_flows count: {len(alt_flows)}")
    for af in alt_flows[:3]:
        print(f"    - {af.get('name','')}: {af.get('trigger','')} → {af.get('expect','')}")
    record("flows happy_path 完整", "PASS" if len(happy_steps) >= 2 else "WARN",
           f"happy_path steps={len(happy_steps)}")
    record("flows alternate_flows 完整", "PASS" if len(alt_flows) >= 1 else "WARN",
           f"alternate_flows count={len(alt_flows)}")
else:
    record("flows 类型", "FAIL", f"expected dict, got {type(flows).__name__}")

# 其他 SRR 字段质量
print(f"\nbusiness_rules: {len(srr.get('business_rules', []))}")
print(f"data_constraints: {len(srr.get('data_constraints', []))}")
print(f"boundary_conditions: {len(srr.get('boundary_conditions', []))}")
print(f"preconditions: {len(srr.get('preconditions', []))}")
print(f"acceptance_criteria: {len(srr.get('acceptance_criteria', []))}")
print(f"test_points_hint: {len(srr.get('test_points_hint', []))}")

record("business_rules >= 1", "PASS" if len(srr.get("business_rules", [])) >= 1 else "WARN",
       f"count={len(srr.get('business_rules', []))}")
record("data_constraints >= 1", "PASS" if len(srr.get("data_constraints", [])) >= 1 else "WARN",
       f"count={len(srr.get('data_constraints', []))}")
record("boundary_conditions >= 3", "PASS" if len(srr.get("boundary_conditions", [])) >= 3 else "WARN",
       f"count={len(srr.get('boundary_conditions', []))}")
record("test_points_hint >= 5", "PASS" if len(srr.get("test_points_hint", [])) >= 5 else "WARN",
       f"count={len(srr.get('test_points_hint', []))}")

# ============ Step 3: FP 列表验证 (正确端点: /fp, 数据键: items) ============
section("Step 3: 功能点（FP）拆分验证")
fp_resp = requests.get(f"{BASE}/requirements/{REQ_ID}/fp", headers=headers, timeout=TIMEOUT)
fp_json = fp_resp.json()
fp_data = fp_json.get("data", {})
fps = fp_data.get("items", []) if isinstance(fp_data, dict) else []

print(f"FP 数量: {len(fps)}")
record("FP 数量 >= 1", "PASS" if len(fps) >= 1 else "FAIL", f"count={len(fps)}")

fp_ids = []
fp_issues = []
for i, fp in enumerate(fps):
    fp_id = fp.get("id", "")
    fp_ids.append(fp_id)
    fp_name = fp.get("name", "")
    fp_type = fp.get("fp_type", "")
    fp_priority = fp.get("priority", "")
    fp_module = fp.get("module_id", "")
    fp_status = fp.get("status", "")
    print(f"  FP[{i}]: id={fp_id}, name={fp_name}, type={fp_type}, priority={fp_priority}, module={fp_module}, status={fp_status}")
    if not fp_name or fp_name == "未命名功能点":
        fp_issues.append(f"FP[{i}] name 为空或未命名")
    if not fp_type:
        fp_issues.append(f"FP[{i}] 缺 fp_type")

record("FP 字段完整性", "PASS" if not fp_issues else "WARN",
       "; ".join(fp_issues) if fp_issues else "所有 FP 字段完整")

# ============ Step 4: 测试用例生成 (SSE, 正确端点: /api/generate) ============
section("Step 4: 测试用例生成（SSE 全流程）")
gen_payload = {"req_id": REQ_ID, "channel": "FULL"}
print(f"发起生成请求: POST /api/generate, req_id={REQ_ID}, channel=FULL")

gen_start = time.time()
try:
    gen_resp = requests.post(
        f"{BASE}/generate",
        json=gen_payload,
        headers=headers,
        stream=True,
        timeout=SSE_TIMEOUT,
    )
except Exception as e:
    record("SSE 连接建立", "FAIL", f"异常: {e}")
    sys.exit(1)

if gen_resp.status_code != 200:
    record("SSE 连接建立", "FAIL", f"HTTP {gen_resp.status_code}: {gen_resp.text[:300]}")
    sys.exit(1)

print("SSE 连接已建立，接收事件流...")
events_received = []
current_event = None
current_data_lines = []
stages_seen = set()
error_events = []
complete_data = None

for raw_line in gen_resp.iter_lines(decode_unicode=True):
    if raw_line is None:
        continue
    if not raw_line:
        # 空行 = 事件结束
        if current_event and current_data_lines:
            data_str = "\n".join(current_data_lines)
            try:
                data = json.loads(data_str) if data_str.startswith("{") or data_str.startswith("[") else data_str
            except json.JSONDecodeError:
                data = data_str
            events_received.append((current_event, data))

            if current_event == "stage":
                stage_name = data.get("stage", "") if isinstance(data, dict) else ""
                stages_seen.add(stage_name)
                msg = data.get("message", "") if isinstance(data, dict) else ""
                print(f"  [stage] {stage_name}: {msg}")
            elif current_event == "progress":
                pct = data.get("progress", 0) if isinstance(data, dict) else 0
                if pct % 25 == 0 or pct == 100:
                    print(f"  [progress] {pct}%")
            elif current_event == "case":
                title = data.get("title", "") if isinstance(data, dict) else ""
                print(f"  [case] {title[:60]}")
            elif current_event == "task_id":
                tid = data if isinstance(data, str) else (data.get("task_id", "") if isinstance(data, dict) else "")
                print(f"  [task_id] {tid}")
            elif current_event == "complete":
                complete_data = data
                print(f"  [complete] 生成完成")
            elif current_event == "error":
                err_msg = data.get("message", "") if isinstance(data, dict) else str(data)
                print(f"  [error] {err_msg}")
                error_events.append(err_msg)
        current_event = None
        current_data_lines = []
        continue

    if raw_line.startswith("event:"):
        current_event = raw_line[6:].strip()
    elif raw_line.startswith("data:"):
        current_data_lines.append(raw_line[5:].lstrip())

gen_elapsed = time.time() - gen_start
print(f"\nSSE 流结束，耗时 {gen_elapsed:.1f}s")
print(f"接收事件总数: {len(events_received)}")
print(f"stages: {stages_seen}")
if complete_data and isinstance(complete_data, dict):
    print(f"complete data keys: {list(complete_data.keys())}")
    cases_from_sse = complete_data.get("cases", [])
    print(f"complete.cases count: {len(cases_from_sse)}")

record("SSE 连接完成", "PASS", f"耗时 {gen_elapsed:.1f}s, 事件 {len(events_received)} 个")
record("SSE 无 error 事件", "PASS" if not error_events else "FAIL",
       "; ".join(error_events) if error_events else "无错误")

# SSE stage 完整性
expected_stages = {"parsing", "generating", "reviewing", "completed", "done", "analyze", "review"}
stages_found = stages_seen & expected_stages
record("SSE 阶段事件", "PASS" if len(stages_found) >= 2 else "WARN",
       f"stages={stages_seen}")

# ============ Step 5: 从 DB 查询生成的用例 (正确端点: /api/testcases) ============
section("Step 5: 查询生成的测试用例")
time.sleep(2)  # 确保 DB 写入完成
cases_resp = requests.get(
    f"{BASE}/testcases",
    headers=headers,
    params={"req_id": REQ_ID, "page_size": 100},
    timeout=TIMEOUT,
)
cases_json = cases_resp.json()
cases_data = cases_json.get("data", {})
cases_list = cases_data.get("items", []) if isinstance(cases_data, dict) else []
total_cases = cases_data.get("total", len(cases_list)) if isinstance(cases_data, dict) else len(cases_list)

print(f"DB 中用例总数: {total_cases}")
record("用例数量 >= 5", "PASS" if total_cases >= 5 else "FAIL", f"total={total_cases}")

# ============ Step 6: 用例质量验证 ============
section("Step 6: 用例质量验证")
if cases_list:
    type_dist = {}
    priorities = {}
    confidence_scores = []
    field_missing = []
    reviewed_pass = 0
    reviewed_total = 0
    fp_linked = 0

    for i, c in enumerate(cases_list):
        ctype = c.get("type", "") or c.get("case_type", "")
        type_dist[ctype] = type_dist.get(ctype, 0) + 1

        pri = c.get("priority", "")
        priorities[pri] = priorities.get(pri, 0) + 1

        conf = c.get("confidence", 0)
        if conf:
            try:
                confidence_scores.append(float(conf))
            except (ValueError, TypeError):
                pass

        required_fields = ["title", "preconditions", "steps", "expected_result"]
        for f in required_fields:
            val = c.get(f, "")
            if not val or (isinstance(val, list) and len(val) == 0):
                field_missing.append(f"case[{i}]({str(c.get('title',''))[:15]}) 缺 {f}")

        review_status = c.get("status", "") or c.get("review_status", "")
        if review_status:
            reviewed_total += 1
            if review_status in ("reviewed", "approved", "pass", "passed"):
                reviewed_pass += 1

        rf = c.get("requirement_field", "")
        if rf and str(rf).startswith("fp:"):
            fp_linked += 1

        if i < 3:
            print(f"\n  用例[{i}]: {c.get('title', '')}")
            print(f"    id: {c.get('id', '')}")
            print(f"    type: {ctype}, priority: {pri}, confidence: {conf}")
            print(f"    status: {review_status}")
            steps = c.get("steps", [])
            if isinstance(steps, list):
                print(f"    steps: {len(steps)} 步")
                for s in steps[:2]:
                    if isinstance(s, dict):
                        print(f"      - {str(s.get('action', s.get('step', '')))[:60]}")
                    else:
                        print(f"      - {str(s)[:60]}")
            expected = c.get("expected_result", "")
            print(f"    expected: {str(expected)[:80]}")
            print(f"    requirement_field: {rf}")

    print(f"\n=== 质量统计 ===")
    print(f"类型分布: {type_dist}")
    print(f"优先级分布: {priorities}")
    if confidence_scores:
        print(f"平均置信度: {sum(confidence_scores)/len(confidence_scores):.3f}")
    print(f"审查通过: {reviewed_pass}/{reviewed_total}")
    print(f"字段缺失: {len(field_missing)} 个")
    print(f"FP 关联: {fp_linked}/{total_cases}")

    record("类型分布 >= 2 种", "PASS" if len(type_dist) >= 2 else "WARN",
           f"types={list(type_dist.keys())}")
    record("用例优先级分布", "PASS" if len(priorities) >= 2 else "WARN",
           f"priorities={list(priorities.keys())}")

    avg_conf = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0
    record("平均置信度 >= 0.7", "PASS" if avg_conf >= 0.7 else "WARN",
           f"avg_conf={avg_conf:.3f}")

    if reviewed_total > 0:
        pass_rate = reviewed_pass / reviewed_total
        record("审查通过率 >= 80%", "PASS" if pass_rate >= 0.8 else "WARN",
               f"pass_rate={pass_rate:.2%} ({reviewed_pass}/{reviewed_total})")
    else:
        record("审查状态", "WARN", "无审查数据")

    record("用例字段完整性", "PASS" if not field_missing else "WARN",
           f"{len(field_missing)} 个缺失" + (": " + "; ".join(field_missing[:3]) if field_missing else ""))

    record("用例关联 FP", "PASS" if fp_linked > 0 else "WARN",
           f"{fp_linked}/{total_cases} 用例关联了 fp_id")
else:
    record("用例查询", "FAIL", "DB 中无用例")

# ============ Step 7: 总结 ============
section("测试总结")
print(f"PASS: {results['pass']}, WARN: {results['warn']}, FAIL: {results['fail']}")
print(f"\n详细结果:")
for item in results["items"]:
    print(f"  [{item['status']}] {item['name']}: {item['detail']}")

out_path = os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_v171_quality.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({
        "req_id": REQ_ID,
        "results": results,
        "fp_ids": fp_ids,
        "cases_count": total_cases,
        "gen_elapsed_sec": round(gen_elapsed, 1),
    }, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存: {out_path}")

sys.exit(0 if results["fail"] == 0 else 1)
