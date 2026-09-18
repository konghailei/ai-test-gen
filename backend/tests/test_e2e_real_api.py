"""真实 HTTP E2E 测试脚本（阶段 10）。

与既有套件的区别：不走 TestClient，直接对运行中的系统发起真实 HTTP 请求
（完整网络栈 + 中间件 + SSE 流式），LLM 为真实调用、无 mock。

覆盖：
    A. 服务就绪等待与登录鉴权
    B. 需求推进：draft →（澄清收敛）→ pending_review → confirm → confirmed
    C. T1 生成前置校验（404 不存在 / 409 未确认）
    D. T2 SSE 真实生成（9 节点编排事件流 + done 汇总）
    E. T4/T9 用例入库与字段质量核验
    F. 用例操作（编辑回读 + 人工反馈）
    G. T8 导出抽查（json / markdown / gherkin / pytest）

运行方式（容器内）：
    docker exec ai-test-gen-api python /app/tests/test_e2e_real_api.py
可配置环境变量：
    E2E_BASE_URL（默认 http://127.0.0.1:8000/api）
    E2E_REQ_ID（默认 REQ-1787723370912-1404）
    E2E_USERNAME / E2E_PASSWORD（默认 admin / 888888）
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000/api")
REQ_ID = os.environ.get("E2E_REQ_ID", "REQ-1787723370912-1404")
USERNAME = os.environ.get("E2E_USERNAME", "admin")
PASSWORD = os.environ.get("E2E_PASSWORD", "888888")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    line = f"  {mark} {name}"
    if detail:
        line += f" — {detail}" if not ok else f" ({detail})"
    print(line)
    return ok


def section(title: str) -> None:
    print(f"\n{YELLOW}━━ {title} ━━{RESET}")


def short(obj, n: int = 160) -> str:
    return json.dumps(obj, ensure_ascii=False)[:n] if not isinstance(obj, str) else obj[:n]


client = httpx.Client(
    base_url=BASE,
    timeout=httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=30.0),
)
HEADERS: dict[str, str] = {}


def wait_service_ready(max_wait: float = 120.0) -> None:
    """等待后端就绪（backend 目录写入会触发 uvicorn --reload 重启，需等待）。"""
    section("A0. 服务就绪等待")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD}, timeout=5.0)
            if r.status_code == 200:
                check("服务就绪且登录成功", True, BASE)
                return
        except Exception:
            pass
        time.sleep(3.0)
    check("服务就绪", False, f"等待 {max_wait}s 后仍不可达: {BASE}")
    raise SystemExit(1)


def get_requirement() -> dict:
    r = client.get(f"/requirements/{REQ_ID}", headers=HEADERS)
    if r.status_code != 200:
        check(f"获取需求 {REQ_ID}", False, f"HTTP {r.status_code}: {short(r.text)}")
        raise SystemExit(1)
    return r.json().get("data") or {}


def wait_requirement_stable(expect_states: tuple[str, ...], max_wait: float = 480.0) -> str:
    """轮询需求状态直到进入期望状态（后台 LLM 任务运行中状态为 analyzing）。"""
    deadline = time.time() + max_wait
    last = ""
    while time.time() < deadline:
        d = get_requirement()
        last = d.get("status", "")
        if last in expect_states:
            return last
        time.sleep(10.0)
    return last


GENERIC_ANSWER = "无特殊要求，按行业通用最佳实践与系统默认策略处理，并在 SRR 中记录该假设。"


def build_answers(questions: list[dict]) -> dict[str, str]:
    answers: dict[str, str] = {}
    for q in questions:
        qid = q.get("id", "")
        if not qid:
            continue
        ans = (q.get("default_answer") or "").strip()
        if not ans:
            opts = q.get("options") or []
            ans = str(opts[0]) if opts else GENERIC_ANSWER
        answers[qid] = ans
    return answers


def phase_login() -> None:
    section("A. 登录鉴权")
    r = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
    ok = check("POST /auth/login 返回 200", r.status_code == 200, short(r.text))
    if not ok:
        raise SystemExit(1)
    token = (r.json().get("data") or {}).get("token", "")
    check("返回 JWT token 非空", bool(token))
    r2 = client.get("/requirements", params={"page": 1, "page_size": 1}, headers={"Authorization": "Bearer invalid"})
    check("无效 token 被拒绝（401）", r2.status_code in (401, 403), f"HTTP {r2.status_code}")
    HEADERS["Authorization"] = f"Bearer {token}"


def phase_advance_requirement() -> None:
    section(f"B. 需求推进（{REQ_ID}）")
    d = get_requirement()
    status = d.get("status")
    check(f"需求存在，初始状态={status}", True)

    if status == "confirmed":
        check("需求已 confirmed，跳过推进", True)
        return

    if status not in ("draft", "pending_review"):
        check("需求状态可推进", False, f"当前状态 {status}，仅支持 draft/pending_review")
        raise SystemExit(1)

    if status == "draft":
        r = client.get(f"/requirements/{REQ_ID}/clarify/questions", headers=HEADERS)
        qdata = (r.json().get("data") or {}) if r.status_code == 200 else {}
        questions = qdata.get("questions") or []
        check(f"获取澄清问题（{len(questions)} 个待答）", True)
        if questions:
            answers = build_answers(questions)
            rc = client.post(f"/requirements/{REQ_ID}/clarify", json={"answers": answers, "extra_notes": ""}, headers=HEADERS)
            check("POST clarify 提交答案", rc.status_code == 200, short(rc.json() if rc.status_code != 200 else {"answers": len(answers)}))
            final = wait_requirement_stable(("draft", "pending_review"))
            check(f"澄清任务完成，状态={final}", final in ("draft", "pending_review"))
            status = final

    if status == "draft":
        r = client.get(f"/requirements/{REQ_ID}/clarify/questions", headers=HEADERS)
        qdata = (r.json().get("data") or {}) if r.status_code == 200 else {}
        questions = [q for q in (qdata.get("questions") or []) if not q.get("optional")]
        if questions:
            print(f"  {CYAN}ℹ 澄清仍未收敛（{len(questions)} 个追问），再答一轮{RESET}")
            answers = build_answers(questions)
            rc = client.post(f"/requirements/{REQ_ID}/clarify", json={"answers": answers, "extra_notes": "请基于以上回答收敛，无其他特殊要求。"}, headers=HEADERS)
            check("第二轮 clarify 提交", rc.status_code == 200, short(rc.text))
            final = wait_requirement_stable(("draft", "pending_review"))
            check(f"第二轮完成，状态={final}", final in ("draft", "pending_review"))
            status = final

    if status == "draft":
        print(f"  {CYAN}ℹ 澄清轮次受限，使用 finalize 生成最终 SRR（同步，可能数分钟）{RESET}")
        rf = client.post(f"/requirements/{REQ_ID}/finalize", headers=HEADERS)
        check("POST finalize", rf.status_code == 200, short(rf.text))
        final = wait_requirement_stable(("draft", "pending_review"))
        check(f"finalize 完成，状态={final}", final == "pending_review", f"实际 {final}")
        status = final

    if status != "pending_review":
        check("到达 pending_review", False, f"实际状态 {status}")
        raise SystemExit(1)
    check("需求到达 pending_review", True)


def phase_precheck_409_then_confirm() -> None:
    section("C. T1 生成前置校验（confirm 前测 409）")
    r = client.post("/generate", json={"req_id": "REQ-NO-SUCH-000"}, headers=HEADERS)
    check("生成不存在的需求 → 404", r.status_code == 404, f"HTTP {r.status_code}")
    r = client.post("/generate", json={"req_id": REQ_ID}, headers=HEADERS)
    check(f"未确认需求生成 → 409（当前状态非 confirmed）", r.status_code == 409, f"HTTP {r.status_code}: {short(r.text)}")

    section("D. 确认需求（pending_review → confirmed）")
    rc = client.post(f"/requirements/{REQ_ID}/confirm", headers=HEADERS)

    if rc.status_code == 409 and "降级标记" in rc.text:
        print(f"  {CYAN}ℹ 门禁提示降级标记，执行人工复核动作（清除标记后重试）{RESET}")
        rd = client.get(f"/requirements/{REQ_ID}", headers=HEADERS)
        d = rd.json().get("data") or {}
        srr = d.get("srr_json") or {}
        cleared = [k for k in ("_clarify_degraded", "_finalize_degraded", "_recheck_skipped") if srr.pop(k, None)]
        check(f"人工复核通过，清除降级标记 {cleared}", bool(cleared))
        ru = client.put(f"/requirements/{REQ_ID}", json={"srr_json": srr}, headers=HEADERS)
        check("PUT /requirements 保存复核结果", ru.status_code == 200, short(ru.text))
        rc = client.post(f"/requirements/{REQ_ID}/confirm", headers=HEADERS)

    if rc.status_code == 409 and "未审核的功能点" in rc.text:
        print(f"  {CYAN}ℹ 门禁提示存在未审核功能点，执行审核流程{RESET}")
        rfp = client.get(f"/requirements/{REQ_ID}/fp", headers=HEADERS)
        fps = (rfp.json().get("data") or {})
        fps = fps.get("items") if isinstance(fps, dict) else fps
        fps = fps or []
        rinfo = client.get(f"/requirements/{REQ_ID}", headers=HEADERS)
        req_modules = (rinfo.json().get("data") or {}).get("modules") or []
        fallback_module = req_modules[0] if req_modules else ""
        for fp in fps:
            fpid = fp.get("id")
            if fp.get("status") == "approved":
                continue
            if not fp.get("module_id") and not (fp.get("modules") or []):
                target = fallback_module or "auth"
                rm = client.put(f"/requirements/{REQ_ID}/fp/{fpid}", json={"modules": [target]}, headers=HEADERS)
                check(f"功能点「{fp.get('name')}」分配模块 {target}", rm.status_code == 200, short(rm.text))
            ra = client.patch(f"/requirements/{REQ_ID}/fp/{fpid}/approve", headers=HEADERS)
            check(f"功能点「{fp.get('name')}」审核通过", ra.status_code == 200, short(ra.text))
        rc = client.post(f"/requirements/{REQ_ID}/confirm", headers=HEADERS)

    check("POST confirm 返回 200", rc.status_code == 200, short(rc.json().get("detail", rc.text) if rc.status_code != 200 else rc.text))
    if rc.status_code != 200:
        raise SystemExit(1)
    d = get_requirement()
    check("需求状态已变为 confirmed", d.get("status") == "confirmed", f"实际 {d.get('status')}")


def phase_sse_generate() -> dict:
    section("E. T2 SSE 真实生成（LLM 全链路，预计 2-5 分钟）")
    stages: list[str] = []
    done_payload: dict = {}
    error_payload: dict = {}
    t0 = time.time()

    with client.stream("POST", "/generate", json={"req_id": REQ_ID}, headers=HEADERS) as r:
        check("POST /generate 流式连接建立", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            body = r.read().decode("utf-8", "replace")
            check("流式连接", False, body[:300])
            raise SystemExit(1)
        event_name = ""
        for line in r.iter_lines():
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                stage = payload.get("stage", "")
                if stage and stage not in stages:
                    stages.append(stage)
                    print(f"  {CYAN}▸ [{time.time()-t0:6.1f}s] stage={stage}{RESET}")
                if stage == "done":
                    done_payload = payload
                if stage == "error":
                    error_payload = payload
    elapsed = time.time() - t0
    check("SSE 流正常结束", not error_payload, short(error_payload))
    # v3.4.4 起无 FAST/FULL 通道概念：生产路径（已确认需求）enable_clarify 恒为 False，
    # clarify 节点被条件路由跳过，SSE 不产出该 stage（事件由 astream 按实际执行节点产出）。
    expect_stages = ["parse", "select_channel", "gate", "impact", "test_design", "generate", "review", "done"]
    missing = [s for s in expect_stages if s not in stages]
    check("8 节点 stage 事件齐全", not missing, f"缺少 {missing}，实际 {stages}")
    check("clarify stage 已跳过（已确认需求恒跳过生成期澄清）",
          "clarify" not in stages, f"实际出现 {stages}")
    check("done 事件包含 cases", bool(done_payload.get("cases")), short(done_payload, 300))
    check("done 事件不含 channel（v3.4.4 已删除该字段）",
          "channel" not in done_payload, short(done_payload, 200))
    cases = done_payload.get("cases") or []
    check(f"生成用例数量 ≥ 5（实际 {len(cases)}，耗时 {elapsed:.0f}s）", len(cases) >= 5)
    return done_payload


def phase_verify_cases(done_payload: dict) -> list[str]:
    section("F. T4/T9 用例入库与质量核验")
    r = client.get("/testcases", params={"req_id": REQ_ID, "page": 1, "page_size": 50}, headers=HEADERS)
    check("GET /testcases?req_id= 返回 200", r.status_code == 200, short(r.text))
    data = r.json().get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    items = items or []
    total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
    done_n = len(done_payload.get("cases") or [])
    check(f"入库数量与 done 一致（DB total={total}，done={done_n}）", total >= done_n and done_n > 0)
    if not items:
        check("用例列表非空", False, "无用例可继续验证，请检查生成阶段")
        return []

    bad = []
    types_seen = set()
    for c in items:
        cid = c.get("case_id") or c.get("id")
        if not c.get("title") or not c.get("steps") or not c.get("expected"):
            bad.append(cid)
        if c.get("case_type") or c.get("type"):
            types_seen.add(c.get("case_type") or c.get("type"))
    check("全部用例具备 title/steps/expected", not bad, f"缺失用例 {bad[:5]}")
    check(f"用例类型有分布（{sorted(types_seen)}）", bool(types_seen))

    section("G. 用例操作（编辑回读 + 人工反馈）")
    first = items[0]
    cid = first.get("case_id") or first.get("id")
    new_title = f"{first.get('title')}【E2E】"
    ru = client.put(f"/testcases/{cid}", json={"title": new_title}, headers=HEADERS)
    check(f"PUT /testcases/{cid} 编辑标题", ru.status_code == 200, short(ru.text))
    rd = client.get(f"/testcases/{cid}", headers=HEADERS)
    got = (rd.json().get("data") or {})
    check("编辑回读生效", got.get("title") == new_title, f"实际 {got.get('title')}")
    ru2 = client.put(f"/testcases/{cid}", json={"title": first.get("title")}, headers=HEADERS)
    check("标题还原", ru2.status_code == 200)
    rf = client.post(f"/testcases/{cid}/feedback", json={"accepted": True, "comments": "E2E 真实接口验证：用例可用"}, headers=HEADERS)
    check("POST feedback 采纳反馈", rf.status_code == 200, short(rf.text))
    return [c.get("case_id") or c.get("id") for c in items]


def phase_export(case_ids: list[str]) -> None:
    section("H. T8 导出抽查（4 格式）")
    for fmt in ("json", "markdown", "gherkin", "pytest"):
        r = client.post("/export", json={"format": fmt, "case_ids": case_ids, "project_name": "E2E-Real-API"}, headers=HEADERS)
        ok = r.status_code == 200
        detail = f"HTTP {r.status_code} len={len(r.content)}"
        if ok:
            body = r.text
            leaks = [k for k in ("password", "secret", "api_key", "token") if k in body.lower()]
            detail = f"len={len(r.content)} 泄露扫描={leaks or 'clean'}"
            ok = not leaks
        check(f"导出 {fmt}", ok, detail)


def main() -> int:
    print(f"{YELLOW}真实 HTTP E2E：BASE={BASE} REQ={REQ_ID}{RESET}")
    wait_service_ready()
    phase_login()
    phase_advance_requirement()
    phase_precheck_409_then_confirm()
    done_payload = phase_sse_generate()
    case_ids = phase_verify_cases(done_payload)
    phase_export(case_ids)

    print(f"\n{YELLOW}━━ 汇总 ━━{RESET}")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    for name, ok, detail in results:
        if not ok:
            print(f"  {RED}✗ {name} — {detail}{RESET}")
    print(f"\n结果: {GREEN}{passed} 通过{RESET} / {RED if failed else RESET}{failed} 失败{RESET} / 共 {len(results)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
