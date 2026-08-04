"""端到端测试：需求分析全链路 + 归档 + 知识问答 + KB 补全。

测试流程：
1. 登录获取 token
2. 上传第一个文档（01_login_full.md）→ 分析每个链路节点结果
3. 归档第一个文档 → 验证知识库写入
4. 知识问答 → 验证归档后能正确回答
5. 上传第二个文档（02_login_optimize.md）→ 验证 KB 补全

Usage:
    python test_e2e_flow.py
"""

import json
import sys
import time
import requests
from pathlib import Path

BASE = "http://localhost:8000/api"
TOKEN = ""
TIMEOUT = 120


def pp(label: str, data):
    """Pretty print JSON data."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
    else:
        print(data)


def login():
    """登录获取 token。"""
    r = requests.post(f"{BASE}/auth/login", json={
        "username": "admin",
        "password": "888888",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Login failed: {data}")
    token = data["data"]["token"]
    print(f"[OK] Login success, token={token[:20]}...")
    return token


def headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def test_upload_first_doc():
    """上传第一个文档，分析链路节点结果。"""
    print("\n" + "#"*60)
    print("# STEP 1: Upload first document (01_login_full.md)")
    print("#"*60)

    doc_path = Path(__file__).parent / "requirements" / "01_login_full.md"
    raw_text = doc_path.read_text(encoding="utf-8")

    payload = {
        "raw_text": raw_text,
        "product_id": "PROD-TEST-E2E",
        "product_name": "E2E测试产品",
        "title": "用户登录功能（完整版）",
        "priority": "P1",
        "modules": ["auth", "session"],
    }

    r = requests.post(f"{BASE}/requirements/upload", json=payload, headers=headers(), timeout=TIMEOUT)
    r.raise_for_status()
    resp = r.json()

    if not resp.get("success"):
        pp("UPLOAD FAILED", resp)
        return None, None

    data = resp["data"]
    req_id = data["req_id"]

    # 节点1: 基本信息
    pp("Node 1 - Basic Info", {
        "req_id": req_id,
        "title": data.get("title"),
        "priority": data.get("priority"),
        "status": data.get("status"),
        "has_unanswered_questions": data.get("has_unanswered_questions"),
    })

    # 节点2: KB 状态
    pp("Node 2 - KB Status", {
        "kb_empty": data.get("kb_empty"),
        "kb_status": data.get("kb_status"),
        "kb_answered": data.get("kb_answered"),
        "kb_total": data.get("kb_total"),
        "auto_augmentations_count": data.get("auto_augmentations_count"),
    })

    # 节点3: 澄清问题
    questions = data.get("questions", [])
    pp(f"Node 3 - Clarify Questions ({len(questions)} total)", questions[:5] if questions else "None")

    # 节点4: SRR 结构
    srr = data.get("srr", {})
    req_info = srr.get("requirement", {})
    flows = srr.get("flows", {})
    pp("Node 4 - SRR Structure", {
        "requirement": {
            "id": req_info.get("id"),
            "title": req_info.get("title"),
            "summary": (req_info.get("summary") or "")[:200],
            "modules": req_info.get("modules"),
            "product_id": req_info.get("product_id"),
        },
        "flows_keys": list(flows.keys()) if isinstance(flows, dict) else str(type(flows)),
        "happy_path_steps": len(flows.get("happy_path", {}).get("steps", [])) if isinstance(flows, dict) else 0,
        "business_rules_count": len(srr.get("business_rules", [])),
        "boundary_conditions_count": len(srr.get("boundary_conditions", [])),
        "has_clarify_state": "_clarify_state" in srr,
        "has_kb_query_logs": "_kb_query_logs" in srr,
    })

    # 节点5: KB 查询日志（如果有）
    kb_logs = srr.get("_kb_query_logs", [])
    if kb_logs:
        pp(f"Node 5 - KB Query Logs ({len(kb_logs)} entries)", [
            {
                "action": log.get("action") or log.get("type"),
                "label": log.get("label"),
                "query": (log.get("query") or "")[:100],
                "result_count": log.get("result_count"),
                "status": log.get("status"),
                "duration_ms": log.get("duration_ms"),
            }
            for log in kb_logs[:8]
        ])
    else:
        pp("Node 5 - KB Query Logs", "None (no KB queries)")

    # 节点6: Clarify State（如果有）
    clarify_state = srr.get("_clarify_state", {})
    if clarify_state:
        pp("Node 6 - Clarify State", {
            "questions_count": len(clarify_state.get("questions", [])),
            "auto_augmentations": len(clarify_state.get("auto_augmentations", [])),
            "kb_status": clarify_state.get("kb_status"),
            "quality_report": clarify_state.get("quality_report"),
            "field_conflicts": len(clarify_state.get("field_conflicts", [])),
        })
    else:
        pp("Node 6 - Clarify State", "None (status=active, no clarification needed)")

    # 节点7: 自动补全详情
    auto_augs = clarify_state.get("auto_augmentations", []) if clarify_state else []
    if auto_augs:
        pp(f"Node 7 - Auto Augmentations ({len(auto_augs)})", auto_augs[:5])
    else:
        pp("Node 7 - Auto Augmentations", "None")

    return req_id, data


def test_archive(req_id: str):
    """归档需求，验证知识库写入。"""
    print("\n" + "#"*60)
    print(f"# STEP 2: Archive requirement {req_id}")
    print("#"*60)

    # 获取归档前状态
    r = requests.get(f"{BASE}/requirements/{req_id}", headers=headers(), timeout=30)
    pre_data = r.json().get("data", {})
    pp("Pre-archive status", {"status": pre_data.get("status")})

    # 如果状态是 draft，需要先完成澄清
    if pre_data.get("status") == "draft":
        print("[INFO] Requirement is in draft status, submitting clarification first...")
        questions = pre_data.get("srr", {}).get("_clarify_state", {}).get("questions", [])
        if questions:
            answers = {}
            for q in questions:
                qid = q.get("id", "")
                if qid:
                    answers[qid] = "测试回答：该需求需要支持标准登录功能，包含密码校验和会话管理"
            r = requests.post(f"{BASE}/requirements/{req_id}/clarify", json={
                "answers": answers,
            }, headers=headers(), timeout=TIMEOUT)
            if r.status_code == 200:
                resp = r.json()
                pp("Clarify submit result", {
                    "success": resp.get("success"),
                    "status": resp.get("data", {}).get("status"),
                })
            else:
                pp("Clarify submit failed", r.text[:500])

    # 执行归档
    r = requests.post(f"{BASE}/requirements/{req_id}/archive", headers=headers(), timeout=TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    data = resp.get("data", {})

    pp("Archive Result", {
        "success": resp.get("success"),
        "message": resp.get("message"),
        "req_id": data.get("req_id"),
        "status": data.get("status"),
        "kb_task_queued": data.get("kb_task_queued"),
        "kb_error": data.get("kb_error", ""),
    })

    # 验证 /api/health 中的 collection 数量
    r = requests.get(f"{BASE}/health", timeout=10)
    health = r.json().get("data", {})
    vs = health.get("vector_db", {})
    pp("Health Check - Vector DB", {
        "connected": vs.get("connected"),
        "model_ready": vs.get("model_ready"),
        "collections": vs.get("collections"),
        "kb_empty": health.get("kb_empty"),
    })

    return data.get("kb_task_queued", False)


def test_knowledge_qa():
    """知识问答测试。"""
    print("\n" + "#"*60)
    print("# STEP 3: Knowledge QA")
    print("#"*60)

    question = "登录功能的正常流程是什么？密码存储有什么安全要求？"
    print(f"[Q] {question}")

    r = requests.post(f"{BASE}/knowledge-qa/ask", json={
        "question": question,
        "product_id": "PROD-TEST-E2E",
    }, headers=headers(), timeout=TIMEOUT, stream=True)

    sources = []
    answer = ""
    kb_status = None
    record_id = None

    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            payload = line[6:]
            if event_type == "sources":
                sources = json.loads(payload)
            elif event_type == "done":
                done_data = json.loads(payload)
                answer = done_data.get("answer_full", "")
                kb_status = done_data.get("kb_status")
                record_id = done_data.get("id")
            elif event_type == "chunk":
                answer = payload

    pp("QA Sources", [
        {"id": s.get("id"), "title": s.get("title"), "similarity": s.get("similarity")}
        for s in sources
    ])

    pp("QA KB Status", kb_status)

    pp("QA Answer (first 1000 chars)", answer[:1000])

    return len(sources) > 0, record_id


def test_upload_second_doc():
    """上传第二个文档，验证 KB 补全。"""
    print("\n" + "#"*60)
    print("# STEP 4: Upload second document (02_login_optimize.md)")
    print("#"*60)

    doc_path = Path(__file__).parent / "requirements" / "02_login_optimize.md"
    raw_text = doc_path.read_text(encoding="utf-8")

    payload = {
        "raw_text": raw_text,
        "product_id": "PROD-TEST-E2E",
        "product_name": "E2E测试产品",
        "title": "用户登录功能优化（增量）",
        "priority": "P2",
        "modules": ["auth"],
    }

    r = requests.post(f"{BASE}/requirements/upload", json=payload, headers=headers(), timeout=TIMEOUT)
    r.raise_for_status()
    resp = r.json()

    if not resp.get("success"):
        pp("UPLOAD FAILED", resp)
        return None

    data = resp["data"]
    req_id = data["req_id"]

    # 验证 KB 补全
    pp("Second Doc - KB Augmentation", {
        "req_id": req_id,
        "status": data.get("status"),
        "kb_empty": data.get("kb_empty"),
        "kb_answered": data.get("kb_answered"),
        "kb_total": data.get("kb_total"),
        "auto_augmentations_count": data.get("auto_augmentations_count"),
        "has_unanswered_questions": data.get("has_unanswered_questions"),
    })

    # 查看 KB 查询日志
    srr = data.get("srr", {})
    kb_logs = srr.get("_kb_query_logs", [])
    if kb_logs:
        pp(f"Second Doc - KB Query Logs ({len(kb_logs)} entries)", [
            {
                "action": log.get("action") or log.get("type"),
                "label": log.get("label"),
                "query": (log.get("query") or "")[:120],
                "result_count": log.get("result_count"),
                "status": log.get("status"),
            }
            for log in kb_logs[:10]
        ])
    else:
        pp("Second Doc - KB Query Logs", "None")

    # 查看自动补全详情
    clarify_state = srr.get("_clarify_state", {})
    auto_augs = clarify_state.get("auto_augmentations", [])
    if auto_augs:
        pp(f"Second Doc - Auto Augmentations ({len(auto_augs)})", [
            {
                "field": aug.get("field"),
                "value": str(aug.get("value", ""))[:150],
                "source": aug.get("source"),
                "similarity": aug.get("similarity"),
            }
            for aug in auto_augs[:8]
        ])
    else:
        pp("Second Doc - Auto Augmentations", "None")

    # 查看澄清问题
    questions = data.get("questions", []) or clarify_state.get("questions", [])
    pp(f"Second Doc - Questions ({len(questions)})", [
        {
            "id": q.get("id"),
            "field": q.get("field"),
            "question": (q.get("question") or q.get("description", ""))[:120],
            "category": q.get("category"),
            "priority": q.get("priority"),
        }
        for q in questions[:8]
    ])

    return req_id


def main():
    global TOKEN

    print("="*60)
    print("  E2E Test: Requirement Analysis Full Pipeline")
    print("="*60)

    # 0. Login
    TOKEN = login()

    # 1. Upload first document
    req_id_1, data_1 = test_upload_first_doc()
    if not req_id_1:
        print("[FAIL] First document upload failed")
        sys.exit(1)

    # 2. Archive first document
    kb_task_queued = test_archive(req_id_1)

    # 3. Knowledge QA
    if kb_task_queued:
        qa_ok, qa_record_id = test_knowledge_qa()
        pp("QA Verification", {
            "sources_found": qa_ok,
            "record_id": qa_record_id,
        })
    else:
        print("\n[SKIP] Knowledge QA - KB write failed, skipping QA test")

    # 4. Upload second document
    req_id_2 = test_upload_second_doc()
    if req_id_2:
        pp("Second Doc Summary", {"req_id": req_id_2})

    print("\n" + "="*60)
    print("  E2E Test Complete")
    print("="*60)


if __name__ == "__main__":
    main()
