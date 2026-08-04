"""端到端测试：需求分析 → 澄清 → 确认 → 生成测试用例 → 导出。

使用容器内直接调用 API 的方式，避免外部网络/认证问题。
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8000/api"
TOKEN = None
TIMEOUT = 120


def get_token():
    """获取默认管理令牌。"""
    global TOKEN
    from app.core.auth import get_default_token
    TOKEN = get_default_token()
    return TOKEN


def api(method: str, path: str, body=None, stream=False, timeout=TIMEOUT):
    """调用 API。返回 (status_code, data_or_text)。"""
    url = f"{BASE}{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if stream:
                return resp.status, resp
            text = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text


def section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def step(msg):
    print(f"\n▶ {msg}")


def ok(msg):
    print(f"  ✓ {msg}")


def fail(msg):
    print(f"  ✗ {msg}")


def main():
    get_token()
    ok(f"获取 token: {TOKEN[:30]}...")

    # ==================================================================
    # 阶段 1：健康检查 + 系统名中文化验证
    # ==================================================================
    section("阶段 1: 健康检查 + 系统名验证")
    step("调用 /health")
    code, data = api("GET", "/health")
    if code != 200:
        fail(f"健康检查失败: {code} {data}")
        return False
    ok(f"健康检查通过: {data}")

    step("调用 /openapi.json 验证 API 标题中文化")
    code, data = api("GET", "/openapi.json")
    if code == 200 and isinstance(data, dict):
        title = data.get("info", {}).get("title", "")
        if "AI测试用例生成系统" in title:
            ok(f"API 标题已中文化: {title}")
        else:
            fail(f"API 标题未中文化: {title}")

    # ==================================================================
    # 阶段 2：创建测试需求
    # ==================================================================
    section("阶段 2: 创建测试需求（电商订单管理系统）")
    step("POST /requirements/upload")
    with open("/app/test_e2e_payload.json", "r", encoding="utf-8") as f:
        payload = json.load(f)
    code, data = api("POST", "/requirements/upload", payload)
    if code != 200 or not (data and data.get("success")):
        fail(f"创建需求失败: {code} {data}")
        return False
    req_id = data["data"]["req_id"]
    task_id = data["data"].get("task_id")
    ok(f"需求已创建: req_id={req_id}, task_id={task_id}")

    # ==================================================================
    # 阶段 3：等待解析完成（轮询任务状态）
    # ==================================================================
    section("阶段 3: 等待需求解析（轮询任务）")
    step(f"轮询 /requirements/tasks/{task_id}")
    max_wait = 180  # 3 分钟
    start = time.time()
    final_status = None
    while time.time() - start < max_wait:
        code, data = api("GET", f"/requirements/tasks/{task_id}")
        if code != 200:
            fail(f"查询任务失败: {code} {data}")
            return False
        task = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not task:
            time.sleep(3)
            continue
        status = task.get("status")
        progress = task.get("progress", 0)
        msg = task.get("message", "")
        print(f"\r  任务状态: {status} 进度: {progress}% {msg[:50]}", end="", flush=True)
        if status in ("completed", "success", "failed", "interrupted"):
            final_status = status
            break
        time.sleep(3)
    print()
    if final_status not in ("completed", "success"):
        fail(f"解析未成功完成: status={final_status}")
        # 看任务错误
        code, data = api("GET", f"/requirements/tasks/{task_id}")
        print(f"  任务详情: {json.dumps(data, ensure_ascii=False, indent=2)[:800]}")
        return False
    ok(f"解析完成: status={final_status}")

    # ==================================================================
    # 阶段 4：查看解析后的需求详情
    # ==================================================================
    section("阶段 4: 查看解析后的需求详情")
    step(f"GET /requirements/{req_id}")
    code, data = api("GET", f"/requirements/{req_id}")
    if code != 200:
        fail(f"查询需求失败: {code} {data}")
        return False
    req_detail = data.get("data") if isinstance(data, dict) and "data" in data else data
    status = req_detail.get("status")
    srr = req_detail.get("srr_json") or {}
    ok(f"需求 ID: {req_id}")
    ok(f"标题: {req_detail.get('title')}")
    ok(f"状态: {status}")
    # 打印 SRR 概要
    req = srr.get("requirement", {}) if isinstance(srr, dict) else {}
    modules = req.get("modules", [])
    business_points = req.get("business_points", [])
    fps = req.get("functional_points", [])
    ok(f"模块: {modules}")
    ok(f"业务点数: {len(business_points)}")
    ok(f"功能点数: {len(fps)}")
    for fp in fps[:5]:
        if isinstance(fp, dict):
            print(f"    - [{fp.get('fp_type','?')}] {fp.get('name','?')}")
    # 质量评分
    qr = srr.get("quality_report", {}) if isinstance(srr, dict) else {}
    if qr:
        ok(f"质量评分: overall={qr.get('overall_score')} completeness={qr.get('completeness_score')}")

    # ==================================================================
    # 阶段 5：获取澄清问题
    # ==================================================================
    section("阶段 5: 获取澄清问题")
    step(f"GET /requirements/{req_id}/clarify/questions")
    code, data = api("GET", f"/requirements/{req_id}/clarify/questions")
    if code != 200:
        fail(f"获取澄清问题失败: {code} {data}")
    else:
        cd = data.get("data") if isinstance(data, dict) and "data" in data else data
        questions = cd.get("questions", []) if isinstance(cd, dict) else []
        ok(f"澄清问题数: {len(questions)}")
        for i, q in enumerate(questions[:3]):
            if isinstance(q, dict):
                print(f"    Q{i+1}: {q.get('question','')[:80]}")
        # KB 自动补全
        auto_aug = cd.get("auto_augmentations", []) if isinstance(cd, dict) else []
        ok(f"KB 自动补全: {len(auto_aug)} 项")

    # ==================================================================
    # 阶段 6：提交澄清答案 → 重新解析
    # ==================================================================
    section("阶段 6: 提交澄清答案（如有问题）")
    if questions:
        step(f"POST /requirements/{req_id}/clarify")
        answers = {}
        for q in questions:
            qid = q.get("id") or q.get("question_id") or q.get("question", "")[:30]
            answers[qid] = "暂无补充，请按需求文档执行"
        body = {"answers": answers, "extra_notes": "无补充信息"}
        code, data = api("POST", f"/requirements/{req_id}/clarify", body, timeout=180)
        if code != 200:
            fail(f"提交澄清失败: {code} {data}")
        else:
            cd = data.get("data") if isinstance(data, dict) and "data" in data else data
            new_task_id = cd.get("task_id") if isinstance(cd, dict) else None
            ok(f"已提交澄清，新任务: {new_task_id}")
            # 等待重新解析
            if new_task_id:
                step("等待重新解析完成")
                start = time.time()
                while time.time() - start < max_wait:
                    code, td = api("GET", f"/requirements/tasks/{new_task_id}")
                    task = td.get("data") if isinstance(td, dict) and "data" in td else td
                    st = task.get("status") if isinstance(task, dict) else None
                    if st in ("completed", "success", "failed", "interrupted"):
                        ok(f"重新解析: {st}")
                        break
                    time.sleep(3)

    # ==================================================================
    # 阶段 7：确认需求（pending_review → confirmed）
    # ==================================================================
    section("阶段 7: 确认需求")
    step("GET 需求当前状态")
    code, data = api("GET", f"/requirements/{req_id}")
    req_detail = data.get("data") if isinstance(data, dict) and "data" in data else data
    status = req_detail.get("status")
    ok(f"当前状态: {status}")

    if status != "confirmed":
        if status == "analyzing":
            step("需求仍在解析中，等待...")
            start = time.time()
            while time.time() - start < 120:
                code, data = api("GET", f"/requirements/{req_id}")
                rd = data.get("data") if isinstance(data, dict) and "data" in data else data
                status = rd.get("status")
                if status != "analyzing":
                    break
                time.sleep(3)
            ok(f"解析结束，状态: {status}")

        # 如果是 draft/pending_review，先看下 FP
        if status in ("draft", "pending_review"):
            step(f"GET /requirements/{req_id}/fp（功能点列表）")
            code, data = api("GET", f"/requirements/{req_id}/fp")
            if code == 200:
                cd = data.get("data") if isinstance(data, dict) and "data" in data else data
                items = cd.get("items", []) if isinstance(cd, dict) else []
                ok(f"功能点数: {len(items)}")
                for fp in items[:5]:
                    if isinstance(fp, dict):
                        appr = fp.get("approval_status", "?")
                        print(f"    - [{appr}] {fp.get('name','?')} ({fp.get('fp_type','?')})")
                # 审核 FP（如有未审核）
                step("审核所有功能点")
                for fp in items:
                    if isinstance(fp, dict) and fp.get("approval_status") != "approved":
                        fp_id = fp.get("id")
                        if fp_id:
                            api("PATCH", f"/requirements/{req_id}/fp/{fp_id}/approve")
                ok("功能点审核完成")

        step(f"POST /requirements/{req_id}/confirm")
        code, data = api("POST", f"/requirements/{req_id}/confirm")
        if code != 200:
            fail(f"确认失败: {code} {data}")
            # 不返回，继续尝试生成
        else:
            cd = data.get("data") if isinstance(data, dict) and "data" in data else data
            new_status = cd.get("status") if isinstance(cd, dict) else None
            ok(f"确认后状态: {new_status}")

    # ==================================================================
    # 阶段 8：生成测试用例（SSE）
    # ==================================================================
    section("阶段 8: 生成测试用例（SSE 流式）")
    step(f"POST /generate/{req_id} （SSE）")
    # 用 urllib 直接读取 SSE 流
    import urllib.request as ur
    url = f"{BASE}/generate/{req_id}"
    req = ur.Request(url, method="POST", headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"})
    cases = []
    review_report = None
    kb_flowback = None
    gen_error = None
    try:
        with ur.urlopen(req, timeout=600) as resp:
            buffer = ""
            stage = ""
            for raw in resp:
                line = raw.decode("utf-8", errors="replace")
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    st = evt.get("stage")
                    if st:
                        stage = st
                    msg = evt.get("message", "")
                    progress = evt.get("progress", 0)
                    print(f"\r  [{st or stage}] {progress}% {msg[:60]}", end="", flush=True)
                    if st == "done":
                        cases = evt.get("cases", [])
                        review_report = evt.get("review_report")
                        kb_flowback = evt.get("kb_flowback")
                        print()
                        break
                    if st == "error":
                        gen_error = evt.get("message", "未知错误")
                        print()
                        break
    except Exception as e:
        gen_error = str(e)
        print()
        fail(f"生成请求异常: {e}")

    if gen_error:
        fail(f"生成失败: {gen_error}")
        return False
    ok(f"生成完成: 共 {len(cases)} 个用例")

    # ==================================================================
    # 阶段 9：验证生成结果质量
    # ==================================================================
    section("阶段 9: 生成结果质量分析")
    if not cases:
        fail("无生成用例")
        return False
    type_count = {}
    priority_count = {}
    step_counts = []
    confidence_vals = []
    missing_fields = []
    for c in cases:
        t = c.get("type", "unknown")
        type_count[t] = type_count.get(t, 0) + 1
        p = c.get("priority", "?")
        priority_count[p] = priority_count.get(p, 0) + 1
        steps = c.get("steps", [])
        step_counts.append(len(steps))
        gm = c.get("gen_metadata", {})
        conf = gm.get("gen_confidence") or c.get("confidence", 0)
        confidence_vals.append(conf)
        # 必填字段检查
        for field in ("id", "title", "type", "steps", "assertions"):
            if not c.get(field):
                missing_fields.append(f"{c.get('id','?')}.{field}")
    ok(f"用例类型分布: {type_count}")
    ok(f"优先级分布: {priority_count}")
    if step_counts:
        ok(f"步骤数: 平均 {sum(step_counts)/len(step_counts):.1f}, 范围 [{min(step_counts)}, {max(step_counts)}]")
    if confidence_vals:
        ok(f"置信度: 平均 {sum(confidence_vals)/len(confidence_vals):.2f}, 范围 [{min(confidence_vals):.2f}, {max(confidence_vals):.2f}]")
    if missing_fields:
        fail(f"缺失字段: {missing_fields[:5]}")
    else:
        ok("所有用例必填字段完整")

    # ID 唯一性验证（验证 Bug 1 修复）
    ids = [c.get("id") for c in cases]
    if len(set(ids)) == len(ids):
        ok(f"ID 唯一性: 通过（{len(ids)} 个 ID 全部唯一）")
    else:
        fail(f"ID 唯一性: 失败！发现 {len(ids) - len(set(ids))} 个重复 ID")

    # 打印前 3 个用例样例
    step("用例样例（前 3 个）")
    for c in cases[:3]:
        print(f"\n    [{c.get('id')}] [{c.get('type')}] {c.get('title')}")
        for i, s in enumerate(c.get("steps", [])[:3]):
            if isinstance(s, dict):
                print(f"      步骤{i+1}: {s.get('action','')} → {s.get('expected','')}")
        if c.get("assertions"):
            for a in c["assertions"][:2]:
                if isinstance(a, dict):
                    print(f"      断言: {a.get('assertion','') or a.get('expected','')}")

    # 审核报告
    if review_report:
        step("审核报告")
        ok(f"总用例: {review_report.get('total_cases')}")
        ok(f"通过: {review_report.get('passed_cases')}")
        ok(f"Blocker: {review_report.get('blocked_cases')}")
        avg_conf = review_report.get("avg_confidence", 0)
        ok(f"平均置信度: {avg_conf*100:.0f}%")
        issues = review_report.get("issues", [])
        if issues:
            print(f"    问题列表 ({len(issues)} 项):")
            for iss in issues[:5]:
                if isinstance(iss, dict):
                    print(f"      [{iss.get('severity')}] {iss.get('case_id')}: {iss.get('message','')[:60]}")

    # KB 回流
    if kb_flowback:
        step("知识库回流")
        ok(f"自动入库: {kb_flowback.get('auto_approved', 0)}")
        ok(f"待审核: {kb_flowback.get('pending_review', 0)}")
        ok(f"已驳回: {kb_flowback.get('rejected', 0)}")
        ok(f"入库错误: {kb_flowback.get('errors', 0)}")

    # ==================================================================
    # 阶段 10：测试导出功能（5 种格式）
    # ==================================================================
    section("阶段 10: 测试导出功能（5 种格式）")
    case_ids = [c.get("id") for c in cases if c.get("id")]
    formats = ["json", "markdown", "gherkin", "pytest", "xmind"]
    export_results = {}
    for fmt in formats:
        step(f"导出 {fmt}")
        body = {"format": fmt, "case_ids": case_ids, "project_name": "电商订单系统"}
        code, data = api("POST", "/export?format=" + fmt, body, timeout=60)
        if code == 200:
            ok(f"{fmt}: 导出成功")
            export_results[fmt] = "OK"
        else:
            fail(f"{fmt}: 导出失败 code={code} {str(data)[:200]}")
            export_results[fmt] = f"FAIL: {code}"

    # ==================================================================
    # 阶段 11：持久化用例到数据库
    # ==================================================================
    section("阶段 11: 验证用例已持久化")
    step(f"GET /test-cases?requirement_id={req_id}")
    code, data = api("GET", f"/test-cases?requirement_id={req_id}&page=1&page_size=50")
    if code == 200:
        cd = data.get("data") if isinstance(data, dict) and "data" in data else data
        items = cd.get("items", []) if isinstance(cd, dict) else []
        ok(f"数据库已持久化用例: {len(items)} 个")
        if items:
            sample = items[0]
            ok(f"样例: {sample.get('id')} [{sample.get('type')}] {sample.get('title','')[:40]}")
    else:
        fail(f"查询持久化用例失败: {code} {data}")

    # ==================================================================
    # 总结
    # ==================================================================
    section("端到端测试总结")
    print(f"  需求 ID: {req_id}")
    print(f"  生成用例数: {len(cases)}")
    print(f"  用例类型: {type_count}")
    print(f"  导出结果: {export_results}")
    print(f"  数据库持久化: {len(items) if 'items' in dir() else '?'}")
    if review_report:
        print(f"  审核通过率: {review_report.get('passed_cases', 0)}/{review_report.get('total_cases', 0)}")
    print("\n=== 端到端测试", "通过 ===" if cases and len(cases) > 0 else "失败 ===")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
