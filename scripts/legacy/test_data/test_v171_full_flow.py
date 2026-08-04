"""v1.7.1 全流程测试：需求分析 → 测试用例生成 → 质量验证。

测试流程：
1. 登录 + LLM 配置验证
2. 上传需求（异步任务模式）→ 轮询任务状态
3. 获取澄清问题 + 提交澄清（异步任务模式）→ 轮询
4. 查询 FP 列表
5. 生成测试用例（SSE 流）
6. 验证生成内容质量（数量、审查通过率、置信度、覆盖度）
"""
import json
import time
import sys
import os
import requests

BASE = "http://localhost:8000/api"
TIMEOUT = 30
TASK_POLL_INTERVAL = 2.0
TASK_POLL_MAX = 120  # 最多轮询 120 次（4 分钟）

DOC = os.path.join(os.path.dirname(__file__), "requirements", "01_login_full.md")


def log(step, msg, level="INFO"):
    print(f"  [{level}] [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def poll_task(headers, task_id, step_label, max_polls=TASK_POLL_MAX):
    """轮询任务状态直到完成。"""
    for i in range(max_polls):
        resp = requests.get(f"{BASE}/requirements/tasks/{task_id}", headers=headers, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  [{step_label}] 轮询失败: {resp.status_code} {resp.text[:100]}")
            time.sleep(TASK_POLL_INTERVAL)
            continue
        data = resp.json().get("data", {})
        status = data.get("status", "")
        phase = data.get("phase", "")
        progress = data.get("progress", 0)
        message = data.get("message", "")
        print(f"  [{step_label}] [{i+1:03d}] status={status} phase={phase} progress={progress}% msg={message[:60]}")
        if status == "completed":
            return data
        elif status in ("failed", "interrupted"):
            print(f"  [{step_label}] [ERROR] 任务失败: {data.get('error', '')}")
            return data
        time.sleep(TASK_POLL_INTERVAL)
    print(f"  [{step_label}] [ERROR] 任务超时")
    return None


def main():
    sep("v1.7.1 全流程测试开始")

    # ============================================================
    # Step 1: 登录 + LLM 配置验证
    # ============================================================
    sep("Step 1: 登录 + LLM 配置")
    resp = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=10)
    assert resp.status_code == 200, f"登录失败: {resp.status_code}"
    token = resp.json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    log("1", "登录成功")

    resp = requests.get(f"{BASE}/settings/llm", headers=headers, timeout=10)
    llm = resp.json()["data"]
    providers = llm.get("providers", [])
    if providers:
        p = providers[0]
        log("1", f"LLM: provider={p['provider']}, model={p['model']}, primary={p['is_primary']}")

    # LLM 连通性测试
    resp = requests.post(f"{BASE}/settings/llm/test?provider_name=qwen", headers=headers, timeout=30)
    test_result = resp.json()
    if test_result.get("success"):
        log("1", f"LLM 连通性: OK ({test_result['data']['latency_ms']}ms)")
    else:
        log("1", f"LLM 连通性: FAILED - {test_result.get('message', '')}", "ERROR")
        sys.exit(1)

    # 获取产品列表
    resp = requests.get(f"{BASE}/products/products", headers=headers, timeout=10)
    products = resp.json().get("data", [])
    if not products:
        log("1", "无产品，创建新产品", "WARN")
        resp = requests.post(f"{BASE}/products/products", json={
            "name": f"全流程测试-{int(time.time())}",
            "description": "v1.7.1 全流程测试"
        }, headers=headers, timeout=10)
        pid = resp.json()["data"]["id"]
    else:
        pid = products[0]["id"]
    log("1", f"使用产品: {pid} ({products[0].get('name', '')[:30]})")

    # ============================================================
    # Step 2: 上传需求（异步任务模式）
    # ============================================================
    sep("Step 2: 上传需求（异步任务模式）")
    with open(DOC, "r", encoding="utf-8") as f:
        content = f.read()
    log("2", f"文档长度: {len(content)}")

    resp = requests.post(f"{BASE}/requirements/upload", json={
        "raw_text": content,
        "product_id": pid,
        "source_type": "markdown",
    }, headers=headers, timeout=30)

    if resp.status_code != 200:
        log("2", f"上传失败: {resp.status_code} {resp.text[:300]}", "ERROR")
        sys.exit(1)

    data = resp.json().get("data", {})
    req_id = data.get("req_id", "")
    task_id = data.get("task_id", "")
    status = data.get("status", "")
    log("2", f"req_id={req_id}")
    log("2", f"task_id={task_id}")
    log("2", f"status={status}")

    if not task_id:
        log("2", "未返回 task_id（可能仍为同步模式）", "WARN")

    # 轮询任务状态
    if task_id:
        sep("Step 2b: 轮询上传任务状态")
        task_result = poll_task(headers, task_id, "2b")
        if not task_result or task_result.get("status") != "completed":
            log("2b", "上传任务未完成", "ERROR")
            sys.exit(1)
        log("2b", "上传任务完成!")

    # ============================================================
    # Step 3: 获取澄清问题
    # ============================================================
    sep("Step 3: 获取澄清问题")
    resp = requests.get(f"{BASE}/requirements/{req_id}/clarify/questions", headers=headers, timeout=30)
    if resp.status_code != 200:
        log("3", f"获取失败: {resp.status_code} {resp.text[:200]}", "WARN")
        questions = []
        quality_report = {}
    else:
        cdata = resp.json().get("data", {})
        questions = cdata.get("questions", [])
        quality_report = cdata.get("quality_report", {})
        kb_status = cdata.get("kb_status", {})
        auto_augs = cdata.get("auto_augmentations", [])
        log("3", f"问题数: {len(questions)}")
        log("3", f"质量评分: {quality_report.get('overall_score', 'N/A')}")
        log("3", f"KB empty: {kb_status.get('kb_empty', 'N/A')}")
        log("3", f"KB 补全: {len(auto_augs)} 项")
        for i, q in enumerate(questions[:3]):
            print(f"    Q{i+1}: [{q.get('priority', '?')}] {q.get('question', '')[:60]}")

    # ============================================================
    # Step 4: 提交澄清答案（异步任务模式）
    # ============================================================
    sep("Step 4: 提交澄清答案")
    if len(questions) == 0:
        log("4", "无澄清问题，跳过提交。需求可能已为 active 状态", "INFO")
        submit_task_id = None
    else:
        answers = {}
        for q in questions:
            fid = q.get("id", "")
            default_ans = q.get("default_answer", "")
            answers[fid] = default_ans if default_ans else "测试答案"

        resp = requests.post(f"{BASE}/requirements/{req_id}/clarify", json={
            "answers": answers,
            "extra_notes": "v1.7.1 全流程测试"
        }, headers=headers, timeout=30)

        if resp.status_code != 200:
            log("4", f"提交失败: {resp.status_code} {resp.text[:300]}", "WARN")
            submit_task_id = None
        else:
            sdata = resp.json().get("data", {})
            submit_task_id = sdata.get("task_id", "")
            log("4", f"submit task_id={submit_task_id}")

            if submit_task_id:
                task_result = poll_task(headers, submit_task_id, "4")
                if task_result and task_result.get("status") == "completed":
                    log("4", "澄清提交任务完成!")

    # ============================================================
    # Step 5: 查询需求详情 + FP 列表
    # ============================================================
    sep("Step 5: 查询需求详情 + FP 列表")
    resp = requests.get(f"{BASE}/requirements/{req_id}", headers=headers, timeout=10)
    if resp.status_code == 200:
        detail = resp.json().get("data", {})
        log("5", f"需求: {detail.get('title', '')[:40]}")
        log("5", f"状态: {detail.get('status', '')}")
        log("5", f"版本: {detail.get('version', '')}")
        srr = detail.get("srr_json", {})
        log("5", f"SRR 字段: {list(srr.keys())[:15]}")

    resp = requests.get(f"{BASE}/requirements/{req_id}/fp", headers=headers, timeout=10)
    if resp.status_code != 200:
        log("5", f"FP 查询失败: {resp.status_code}", "WARN")
        fps = []
    else:
        fpdata = resp.json().get("data", {})
        fps = fpdata.get("items", []) if isinstance(fpdata, dict) else fpdata
        if not fps and isinstance(fpdata, dict):
            fps = fpdata.get("functional_points", [])
        log("5", f"FP 数量: {len(fps)}")
        for i, fp in enumerate(fps[:5]):
            print(f"    FP{i+1}: {fp.get('name', '')[:30]} type={fp.get('fp_type', '')} priority={fp.get('priority', '')}")

    # ============================================================
    # Step 6: 生成测试用例（SSE 流）
    # ============================================================
    sep("Step 6: 生成测试用例（SSE 流）")
    log("6", f"开始 SSE 生成: req_id={req_id}")

    cases = []
    review_report = {}
    sse_events = []

    try:
        resp = requests.post(
            f"{BASE}/generate",
            json={"req_id": req_id},
            headers=headers,
            stream=True,
            timeout=600,
        )
        if resp.status_code != 200:
            log("6", f"生成失败: {resp.status_code} {resp.text[:300]}", "ERROR")
        else:
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                if not line_str.startswith("data:"):
                    continue
                payload = line_str[5:].strip()
                if not payload:
                    continue
                try:
                    evt = json.loads(payload)
                    stage = evt.get("stage", "")
                    progress = evt.get("progress", 0)
                    msg = evt.get("message", "")
                    sse_events.append({"stage": stage, "progress": progress, "msg": msg})
                    print(f"    [{stage}] {progress}% - {msg[:80]}")
                    if evt.get("cases"):
                        cases = evt["cases"]
                    if evt.get("review_report"):
                        review_report = evt["review_report"]
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        log("6", f"SSE 异常: {str(e)[:200]}", "ERROR")

    log("6", f"生成用例数: {len(cases)}")
    if review_report:
        log("6", f"审查报告: total={review_report.get('total_cases', 0)}, passed={review_report.get('passed_cases', 0)}, avg_conf={review_report.get('avg_confidence', 0):.2f}")

    # ============================================================
    # Step 7: 验证生成内容质量
    # ============================================================
    sep("Step 7: 验证生成内容质量")

    # 7.1 用例数量
    log("7.1", f"用例总数: {len(cases)}")
    if len(cases) >= 5:
        log("7.1", "✅ 用例数量达标 (≥5)")
    else:
        log("7.1", f"⚠️ 用例数量偏少 ({len(cases)} < 5)", "WARN")

    # 7.2 用例类型分布
    type_dist = {}
    priority_dist = {}
    for c in cases:
        t = c.get("type", "unknown")
        type_dist[t] = type_dist.get(t, 0) + 1
        p = c.get("priority", "unknown")
        priority_dist[p] = priority_dist.get(p, 0) + 1
    log("7.2", f"类型分布: {type_dist}")
    log("7.2", f"优先级分布: {priority_dist}")

    # 7.3 审查报告
    if review_report:
        total = review_report.get("total_cases", 0)
        passed = review_report.get("passed_cases", 0)
        avg_conf = review_report.get("avg_confidence", 0)
        pass_rate = (passed / total * 100) if total > 0 else 0
        log("7.3", f"审查通过: {passed}/{total} ({pass_rate:.1f}%)")
        log("7.3", f"平均置信度: {avg_conf:.2f}")
        if pass_rate >= 80:
            log("7.3", "✅ 审查通过率达标 (≥80%)")
        else:
            log("7.3", f"⚠️ 审查通过率偏低 ({pass_rate:.1f}% < 80%)", "WARN")
        if avg_conf >= 0.7:
            log("7.3", "✅ 置信度达标 (≥0.7)")
        else:
            log("7.3", f"⚠️ 置信度偏低 ({avg_conf:.2f} < 0.7)", "WARN")

    # 7.4 用例内容完整性
    required_fields = ["title", "type", "priority", "case_json"]
    complete_cases = 0
    for c in cases:
        if all(c.get(f) for f in required_fields):
            complete_cases += 1
    log("7.4", f"完整用例: {complete_cases}/{len(cases)}")
    if complete_cases == len(cases):
        log("7.4", "✅ 所有用例字段完整")

    # 7.5 打印前 5 个用例标题
    log("7.5", "用例标题预览:")
    for i, c in enumerate(cases[:8]):
        title = c.get("title", "")[:60]
        ctype = c.get("type", "")
        cpri = c.get("priority", "")
        print(f"    TC{i+1}: [{ctype}/{cpri}] {title}")

    # 7.6 SSE 流程完整性
    stages_seen = [e["stage"] for e in sse_events]
    expected_stages = ["parse", "generate", "review", "done"]
    missing_stages = [s for s in expected_stages if s not in stages_seen]
    log("7.6", f"SSE 阶段: {stages_seen}")
    if not missing_stages:
        log("7.6", "✅ SSE 流程完整")
    else:
        log("7.6", f"⚠️ 缺少阶段: {missing_stages}", "WARN")

    # ============================================================
    # 总结
    # ============================================================
    sep("v1.7.1 全流程测试完成")
    print(f"  需求 ID: {req_id}")
    print(f"  需求状态: {detail.get('status', 'N/A') if resp.status_code == 200 else 'N/A'}")
    print(f"  FP 数量: {len(fps)}")
    print(f"  澄清问题: {len(questions)}")
    print(f"  生成用例: {len(cases)}")
    if review_report:
        print(f"  审查通过率: {passed}/{total} ({pass_rate:.1f}%)")
        print(f"  平均置信度: {avg_conf:.2f}")
    print(f"  SSE 阶段: {len(sse_events)} 事件")


if __name__ == "__main__":
    main()
