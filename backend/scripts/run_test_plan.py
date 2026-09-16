# -*- coding: utf-8 -*-
"""测试计划执行器（v5.0，docs/TEST_PLAN.md 的配套 driver）。

设计原则（与旧版 12 阶段脚本断言的根本区别）：
    **脚本只判"结构与机制"，内容判定回传给 AI。**

三层模型：
    L1 机制层（脚本断言）——六个探针 + 421 回归 + unit。验证的是机制不变量
       （互斥/幂等/状态守卫/指纹），客观且可重复，机器判比人准；
    L2 流程层（脚本驱动接口）——按计划步骤调真实 API 跑全链路，**只做结构断言**
       （状态码、终态合法性、字段齐全、计数一致），零内容断言；
    L3 内容层（AI 判定）——系统生成的内容（用例/SRR/KB 回答）**原样回传**，
       打包成 ai-review/ 证据包 + AI 判定任务书，由 AI 逐条判定；
       判定结果经 `apply_verdicts.py` 落地 approve/reject。

用法（容器内）：
    # 复用已有生成产物（不触发生成，验证 driver / 重新判定历史 run）
    docker exec ai-test-gen-api python /app/scripts/run_test_plan.py \
        --from-run /app/_review_runs/20260915-065020

    # 真跑一次生成（约 25 分钟，消耗额度）
    docker exec ai-test-gen-api python /app/scripts/run_test_plan.py --req-id REQ-xxx

    # 只跑 L1 机制层（秒级）
    docker exec ai-test-gen-api python /app/scripts/run_test_plan.py --only mechanism

输出：/app/_review_runs/plan-<时间戳>/
    plan-results.json      三层结果汇总（机制/流程 pass；内容层 = 待 AI 判定清单）
    ai-review/cases.json   生成用例全文（AI 判定输入）
    ai-review/run.json     运行证据（done 事件 / 发布决策 / 覆盖口径）
    ai-review/AI-REVIEW-TASK.md  AI 判定任务书（判什么 / 按什么标准 / 交付什么）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REVIEW_ROOT = "/app/_review_runs"
PROBES = [
    "_probe_concurrency.py",     # 并发互斥 + SSE 隔离
    "_probe_outbox.py",          # 出箱三崩溃点收敛
    "_probe_durable.py",         # 断点续跑 + 输入指纹
    "_probe_revision_lock.py",   # 需求编辑乐观锁
    "_probe_checkpoint_saver.py",  # PostgresSaver 落 PG
    "_probe_selfheal.py",        # 任务自愈
    "_probe_evidence_claims.py",  # claims 协议 + 证据链 + answer_status 隔离 + 节点 span
]
REGRESSION = [
    "/app/tests/test_phase2_core.py",
    "/app/tests/test_phase3_agents.py",
    "/app/tests/test_phase4_orchestrator.py",
    "/app/tests/test_phase5_api.py",
]

PASS, FAIL, SKIPPED = "pass", "fail", "skipped"


def _run(cmd: list[str], timeout: int = 1800) -> dict:
    """跑一个子步骤，返回结构化结果（不判内容，只判退出码）。"""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        return {"cmd": " ".join(cmd), "exit_code": proc.returncode,
                "duration_s": round(time.monotonic() - t0, 1),
                "tail": tail, "status": PASS if proc.returncode == 0 else FAIL}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "exit_code": -1, "duration_s": timeout,
                "tail": ["TIMEOUT"], "status": FAIL}
    except Exception as e:  # noqa: BLE001
        return {"cmd": " ".join(cmd), "exit_code": -1, "duration_s": 0,
                "tail": [str(e)[:200]], "status": FAIL}


def step_mechanism() -> dict:
    """L1 机制层：六个探针 + 脚本式回归。脚本断言合理——验证的是机制不变量。"""
    steps = []
    for probe in PROBES:
        r = _run(["python", f"/app/{probe}"])
        steps.append({"id": f"probe:{probe}", "layer": "mechanism", **r})
    for suite in REGRESSION:
        r = _run(["python", suite])
        steps.append({"id": f"regression:{os.path.basename(suite)}", "layer": "mechanism", **r})
    return {"layer": "mechanism", "steps": steps,
            "pass": all(s["status"] == PASS for s in steps)}


def step_unit() -> dict:
    r = _run(["python", "-m", "pytest", "tests", "-q", "--no-header", "-p", "no:cacheprovider"],
             timeout=600)
    return {"layer": "mechanism", "steps": [{"id": "pytest:unit", **r}],
            "pass": r["status"] == PASS}




# ── L2 需求分析链路（v5.1：从需求分析到测试用例生成的完整覆盖）──

DEFAULT_DOC = """# 登录安全策略（测试计划样例 v2.0）

## 1. 功能概述
本需求定义客服工单系统登录安全策略。用户连续登录失败 5 次后账号进入锁定状态，
锁定时长固定为 1 分钟，锁定期间禁止登录并实时提示剩余锁定时间，锁定到期后自动解锁
且失败计数清零。失败计数按账号维度统计，成功登录时清零；密码错误提示统一返回
"账号或密码错误"，不区分账号不存在与密码错误。

## 2. 功能点
1. 登录失败计数与锁定触发（连续失败 5 次 → 锁定 1 分钟）
2. 锁定期间登录拒绝与剩余时间提示
3. 锁定到期自动解锁、失败计数清零
4. 用户名不存在与密码错误提示统一（防枚举）

## 3. 业务规则
- 连续失败第 5 次即触发锁定，提示"账号已锁定，请 1 分钟后再试"
- 锁定期间尝试登录不增加失败计数
- 成功登录清零失败计数
"""


def _login(base: str) -> dict:
    import httpx
    r = httpx.post(f"{base}/api/auth/login",
                   json={"username": "admin", "password": "888888"}, timeout=180)
    r.raise_for_status()
    data = r.json().get("data") or {}
    token = data.get("token") or data.get("access_token")
    return {"Authorization": f"Bearer {token}"}


def _poll_task(base: str, headers: dict, task_id: str, timeout_s: int,
               interval: int = 15) -> dict:
    """轮询 analysis_tasks 直到终态（解析/澄清重解析都是异步任务）。"""
    import httpx
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        r = httpx.get(f"{base}/api/requirements/tasks/{task_id}", headers=headers, timeout=60)
        d = (r.json().get("data") or {})
        status = d.get("status", "")
        if status != last:
            print(f"    任务 {task_id}: {status} {d.get('progress', '')}".rstrip())
            last = status
        if status in ("completed", "failed", "interrupted", "aborted"):
            return {"status": status, "data": d}
        time.sleep(interval)
    return {"status": "timeout", "data": {}}


def step_analysis(base: str, headers: dict, doc_path: str, poll_timeout: int) -> dict:
    """L2 需求分析链路：上传 → 解析 → 澄清回答 → 确认。只做结构断言，内容回传 AI。"""
    import httpx

    steps: list[dict] = []
    doc_text = open(doc_path, encoding="utf-8").read() if os.path.exists(doc_path) else DEFAULT_DOC

    # 1) 上传（异步解析任务）
    r = httpx.post(f"{base}/api/requirements/upload", headers=headers, timeout=120, json={
        "raw_text": doc_text, "product_id": "PROD-53145-856",
        "title": "登录安全策略-测试计划样例", "priority": "P0",
        "modules": ["登录安全"],
    })
    up = (r.json().get("data") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    req_id, task_id = up.get("req_id", ""), up.get("task_id", "")
    steps.append({"id": "analysis:upload", "status": PASS if r.status_code == 200 and req_id else FAIL,
                  "detail": f"req_id={req_id} task_id={task_id}"})
    if not req_id:
        return {"layer": "flow-analysis", "steps": steps, "pass": False, "req_id": None}

    # 2) 轮询解析
    poll = _poll_task(base, headers, task_id, poll_timeout)
    steps.append({"id": "analysis:parse", "status": PASS if poll["status"] == "completed" else FAIL,
                  "detail": f"task={poll['status']}"})

    # 3) 结构断言：SRR / 功能点（FP 在功能点表，经 GET /{req_id}/fp 查询）
    gr = httpx.get(f"{base}/api/requirements/{req_id}", headers=headers, timeout=60)
    req_data = (gr.json().get("data") or {})
    srr = req_data.get("srr_json") or {}
    fpr = httpx.get(f"{base}/api/requirements/{req_id}/fp", headers=headers, timeout=60)
    fps = ((fpr.json().get("data") or {}).get("items")
           or (fpr.json().get("data") or {}).get("functional_points") or [])
    if not fps and isinstance(fpr.json().get("data"), list):
        fps = fpr.json().get("data")
    steps.append({"id": "analysis:srr", "status": PASS if srr.get("requirement") else FAIL,
                  "detail": f"SRR.requirement={'有' if srr.get('requirement') else '缺'}"})
    steps.append({"id": "analysis:functional_points", "status": PASS if len(fps) >= 1 else FAIL,
                  "detail": f"功能点 {len(fps)} 个"})

    # 4) 澄清（有问题则回答并等待重解析；答案给最小占位——内容由 AI 判定的是问题质量）
    q = httpx.get(f"{base}/api/requirements/{req_id}/clarify/questions", headers=headers, timeout=60)
    qdata = (q.json().get("data") or {})
    questions = qdata.get("questions") or []
    if questions:
        answers = {q0.get("id") or q0.get("question_id"): "按需求文档执行" for q0 in questions
                   if (q0.get("id") or q0.get("question_id"))}
        cr = httpx.post(f"{base}/api/requirements/{req_id}/clarify", headers=headers,
                        timeout=120, json={"answers": answers})
        ctask = (cr.json().get("data") or {}).get("task_id", "")
        if ctask:
            poll2 = _poll_task(base, headers, ctask, poll_timeout)
            steps.append({"id": "analysis:clarify", "status": PASS if poll2["status"] == "completed" else FAIL,
                          "detail": f"问题 {len(questions)} 条，重解析 {poll2['status']}"})
        else:
            steps.append({"id": "analysis:clarify", "status": FAIL, "detail": "澄清提交未返回 task_id"})
    else:
        steps.append({"id": "analysis:clarify", "status": SKIPPED, "detail": "解析后无澄清问题（质量分达标）"})

    # 5) finalize（draft → pending_review；confirm 仅接受 pending_review）
    fz = httpx.post(f"{base}/api/requirements/{req_id}/finalize", headers=headers, timeout=120)
    steps.append({"id": "analysis:finalize", "status": PASS if fz.status_code == 200 else FAIL,
                  "detail": f"{fz.status_code} {str((fz.json().get('detail') or ''))[:80]}"})

    # 6) 确认
    cf = httpx.post(f"{base}/api/requirements/{req_id}/confirm", headers=headers, timeout=120)
    cf_data = (cf.json().get("data") or {}) if cf.headers.get("content-type", "").startswith("application/json") else {}
    steps.append({"id": "analysis:confirm", "status": PASS if cf.status_code == 200 else FAIL,
                  "detail": f"{cf.status_code} {str(cf.json().get('message') or cf.text)[:80]}"})

    # 6) 回传材料：需求分析产物（SRR/功能点/澄清问题）存 ai-review，供 AI 判定
    artifacts = {"req_id": req_id, "title": req_data.get("title", ""),
                 "status_after_confirm": cf_data.get("status") or req_data.get("status"),
                 "srr": srr, "functional_points": fps,
                 "clarify_questions": questions}
    return {"layer": "flow-analysis", "steps": steps,
            "pass": all(s["status"] in (PASS, SKIPPED) for s in steps),
            "req_id": req_id, "artifacts": artifacts}


def _structural_checks(run_dir: str) -> list[dict]:
    """L2 流程层结构断言：只验证"系统没有结构性故障"，不评判内容好坏。"""
    checks: list[dict] = []
    run_path = os.path.join(run_dir, "run.json")
    if not os.path.exists(run_path):
        return [{"id": "run.json 存在", "status": FAIL, "detail": f"缺 {run_path}"}]
    with open(run_path, encoding="utf-8") as f:
        run = json.load(f)
    # run.json 真实结构（review_generation_run.build_payload）：
    # done_event / outcome{completion_status,publication} / cases / review_report / coverage
    done_ev = run.get("done_event") or {}
    outcome = run.get("outcome") or {}
    cases = run.get("cases") or []
    review = run.get("review_report") or {}
    coverage = run.get("coverage") or {}
    publication = outcome.get("publication") or done_ev.get("publication") or {}

    def _add(cid: str, ok: bool, detail: str = "") -> None:
        checks.append({"id": cid, "status": PASS if ok else FAIL, "detail": detail})

    _add("done 事件存在", bool(done_ev), "无 done 事件 = 流水线未正常收尾")
    # outcome 的键是 status（= done_event.completion_status 的别名）
    _add("终态合法", outcome.get("status") in
         ("completed", "awaiting_human_review", "failed"),
         f"status={outcome.get('status')}")
    _add("发布决策存在", bool(publication),
         f"decision={publication.get('decision', '缺')}")
    _add("产出用例数 > 0", len(cases) > 0,
         f"cases={len(cases)}（0 用例是否该 blocked → 内容层判定）")
    invalid = (review.get("invalid_point_ids") or [])
    _add("无非法要点引用", not invalid, f"invalid_point_ids={invalid[:3]}")
    _add("覆盖口径已产出", coverage.get("status") in ("mapped", "unknown"),
         f"status={coverage.get('status')}，标注率={coverage.get('annotation_rate')}")
    return checks


def step_flow_and_content(run_dir: str | None, req_id: str | None,
                          note: str) -> tuple[dict, str]:
    """L2 流程层 + L3 内容层：触发/复用一次生成，结构断言后把内容打包给 AI。"""
    steps: list[dict] = []
    if run_dir is None:
        if not req_id:
            return ({"layer": "flow", "steps": [{"id": "e2e", "status": SKIPPED,
                                                 "detail": "未提供 --req-id"}]},
                    "")
        r = _run(["python", "/app/scripts/review_generation_run.py", "--req-id", req_id,
                  "--note", note], timeout=3600)
        steps.append({"id": "e2e:generate+collect", "layer": "flow", **r})
        # review_generation_run.py 的输出目录由其自身打印，取最新的 review run
        runs = sorted((d for d in os.listdir(REVIEW_ROOT)
                       if os.path.isdir(os.path.join(REVIEW_ROOT, d))
                       and not d.startswith("plan-")), reverse=True)
        run_dir = os.path.join(REVIEW_ROOT, runs[0]) if runs else ""
        if r["status"] != PASS:
            return {"layer": "flow", "steps": steps,
                    "pass": False}, run_dir

    checks = _structural_checks(run_dir)
    steps.append({"id": "structural-checks", "layer": "flow",
                  "status": PASS if all(c["status"] == PASS for c in checks) else FAIL,
                  "detail": f"{sum(1 for c in checks if c['status'] == PASS)}/{len(checks)} 项结构断言通过",
                  "checks": checks})
    flow = {"layer": "flow", "steps": steps,
            "pass": all(s["status"] == PASS for s in steps)}

    # L3 内容层：打包回传材料（零内容断言——判定交给 AI）
    ai_dir = ""
    if run_dir and os.path.isdir(run_dir):
        ai_dir = run_dir  # 审阅台产物即回传材料（cases.json / run.json / review.html）
    return flow, ai_dir


def write_ai_task(ai_dir: str, summary: dict) -> None:
    """生成 AI 判定任务书：判什么、按什么标准、交付什么。"""
    task = f"""# AI 内容判定任务书（{summary['run_dir']}）

## 你要判定什么
`cases.json` 里的每条生成用例（以及 `run.json` 的整体产出），按以下标准逐条给出判定：

## 判定标准（与系统口径一致）
1. **可执行合同**：断言含 type/target/expected_value、步骤完整（系统已校验，预期全过）；
2. **需求对齐**：用例场景是否真的来自 SRR 要点（对照 run.json 的 point_coverage 与需求原文）；
3. **场景唯一性**：与其他用例是否语义重复（同场景改写 = 重复；数字边界不同 = 不重复）；
4. **需求口径**：用例中的数值/规则是否与 SRR 原文一致（如锁定次数、时长、提示文案）。

## 判定分级
- `ok`：符合预期（合同完整、场景唯一、与需求口径一致）
- `bad`：与需求矛盾 / 与同批另一条场景重复（备注写与谁重复、建议保留哪条）
- `doubt`：表述含糊、判定标准不清、复用历史用例未确认（备注写需要人工确认什么）

## 交付
verdicts JSON（`{{"items": {{"<case_id>": {{"verdict": "ok|bad|doubt", "note": "理由"}}}}}}`）
→ 保存为同目录 `verdicts-auto.json` → 执行：
    docker exec ai-test-gen-api python /app/scripts/apply_verdicts.py \\
        --run-dir <run_dir> --execute
"""
    with open(os.path.join(ai_dir, "AI-REVIEW-TASK.md"), "w", encoding="utf-8") as f:
        f.write(task)


def main() -> int:
    ap = argparse.ArgumentParser(description="测试计划执行器：脚本判结构，内容回传 AI")
    ap.add_argument("--req-id", default="", help="真跑一次生成的需求 ID（L2/L3）")
    ap.add_argument("--from-run", default="", help="复用已有 review run 目录（不触发生成）")
    ap.add_argument("--only", default="", choices=["", "mechanism", "flow"],
                    help="只跑某一层")
    ap.add_argument("--note", default="", help="写入审阅台的说明")
    ap.add_argument("--doc", default="", help="需求分析样例文档路径（缺省用内置样例）")
    ap.add_argument("--poll-timeout", type=int, default=1800,
                    help="解析/澄清异步任务轮询超时（秒，默认 1800）")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="API 基地址")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(REVIEW_ROOT, f"plan-{ts}")
    os.makedirs(out_dir, exist_ok=True)

    plan: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "layers": {}}

    # L1 机制层
    if args.only in ("", "mechanism"):
        mech = step_mechanism()
        unit = step_unit()
        mech["steps"].extend(unit["steps"])
        mech["pass"] = mech["pass"] and unit["pass"]
        plan["layers"]["mechanism"] = mech

    # L2 流程层 + L3 内容层
    ai_dir = ""
    analysis_artifacts = None
    if args.only in ("", "flow"):
        run_dir = args.from_run or None
        if run_dir and not os.path.isabs(run_dir):
            run_dir = os.path.join(REVIEW_ROOT, run_dir)
        gen_req = args.req_id or None

        # v5.1：未指定需求时走**完整链路**——需求分析（上传→解析→澄清→确认）→ 生成
        if run_dir is None and gen_req is None:
            headers = _login(args.base)
            analysis = step_analysis(args.base, headers, args.doc, args.poll_timeout)
            analysis_artifacts = analysis.get("artifacts")
            plan["layers"]["flow-analysis"] = {
                k: v for k, v in analysis.items() if k != "artifacts"}
            if analysis.get("pass") and analysis.get("req_id"):
                gen_req = analysis["req_id"]

        flow, ai_dir = step_flow_and_content(run_dir, gen_req,
                                             args.note or "测试计划执行")

        # 需求分析产物并入 ai-review（SRR/功能点/澄清问题 → AI 判定需求分析内容）
        if analysis_artifacts and ai_dir:
            with open(os.path.join(ai_dir, "analysis.json"), "w", encoding="utf-8") as f:
                json.dump(analysis_artifacts, f, ensure_ascii=False, indent=2)

        plan["layers"]["flow"] = flow
        plan["layers"]["content"] = {
            "layer": "content",
            "mode": "ai-review",
            "evidence_dir": ai_dir,
            "note": "内容零脚本断言：全部回传 AI 逐条判定，判定后经 apply_verdicts.py 落地",
        }

    plan["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    plan["mechanism_pass"] = plan.get("layers", {}).get("mechanism", {}).get("pass", None)
    plan["flow_pass"] = plan.get("layers", {}).get("flow", {}).get("pass", None)
    with open(os.path.join(out_dir, "plan-results.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print("=" * 70)
    print(f"机制层: {'PASS' if plan['mechanism_pass'] else 'FAIL' if plan['mechanism_pass'] is False else '跳过'}"
          f" | 流程层(结构): {'PASS' if plan['flow_pass'] else 'FAIL' if plan['flow_pass'] is False else '跳过'}")
    if ai_dir:
        write_ai_task(ai_dir, {"run_dir": ai_dir})
        print(f"内容层: 待 AI 判定 → 证据包 {ai_dir}")
        print(f"          任务书 {os.path.join(ai_dir, 'AI-REVIEW-TASK.md')}")
    print(f"结果汇总: {os.path.join(out_dir, 'plan-results.json')}")
    failed = [s["id"] for lay in plan["layers"].values()
              for s in lay.get("steps", []) if s.get("status") == FAIL]
    if failed:
        print("失败步骤: " + "; ".join(failed[:6]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
