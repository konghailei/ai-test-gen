"""E2E test for second document upload with KB augmentation.

Uses a fresh product ID to avoid interference from old test data.
Pre-archives the first document, then uploads the second (incremental) document.
"""
import json
import requests
from pathlib import Path

BASE = "http://localhost:8000/api"
TOKEN = ""
PRODUCT_ID = "PROD-E2E-AUG"


def login():
    r = requests.post(f"{BASE}/auth/login", json={"username": "admin", "password": "888888"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["token"]


def headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def upload_and_archive_first():
    """Upload and archive the first document."""
    print("=" * 60)
    print("STEP 1: Upload and archive first document")
    print("=" * 60)

    raw = Path("requirements/01_login_full.md").read_text(encoding="utf-8")
    r = requests.post(f"{BASE}/requirements/upload", json={
        "raw_text": raw,
        "product_id": PRODUCT_ID,
        "product_name": "KB增强测试",
        "title": "用户登录功能（完整版）",
        "priority": "P1",
        "modules": ["auth", "session"],
    }, headers=headers(), timeout=120)

    resp = r.json()
    if not resp["success"]:
        print(f"FAILED: {resp['message']}")
        return None

    req_id = resp["data"]["req_id"]
    print(f"Upload OK: req_id={req_id}, status={resp['data']['status']}")

    # Archive
    r = requests.post(f"{BASE}/requirements/{req_id}/archive", headers=headers(), timeout=120)
    resp = r.json()
    print(f"Archive: kb_written={resp['data'].get('kb_written')}, error={resp['data'].get('kb_error', '')}")
    return req_id


def upload_second_and_check_augmentation():
    """Upload second document and check KB augmentation."""
    print("\n" + "=" * 60)
    print("STEP 2: Upload second document (incremental) — check KB augmentation")
    print("=" * 60)

    raw = Path("requirements/02_login_optimize.md").read_text(encoding="utf-8")
    r = requests.post(f"{BASE}/requirements/upload", json={
        "raw_text": raw,
        "product_id": PRODUCT_ID,
        "product_name": "KB增强测试",
        "title": "用户登录功能优化（增量）",
        "priority": "P2",
        "modules": ["auth"],
    }, headers=headers(), timeout=120)

    resp = r.json()
    if not resp["success"]:
        print(f"FAILED: {resp['message']}")
        return

    data = resp["data"]
    srr = data.get("srr", {})

    print(f"\nreq_id: {data['req_id']}")
    print(f"status: {data['status']}")
    print(f"kb_empty: {data.get('kb_empty')}")
    print(f"kb_answered: {data.get('kb_answered')}")
    print(f"kb_total: {data.get('kb_total')}")
    print(f"auto_augmentations_count: {data.get('auto_augmentations_count')}")
    print(f"has_unanswered_questions: {data.get('has_unanswered_questions')}")

    # KB query logs
    kb_logs = srr.get("_kb_query_logs", [])
    print(f"\n--- KB Query Logs ({len(kb_logs)} entries) ---")
    for log in kb_logs:
        action = log.get("action") or log.get("type", "")
        label = log.get("label", "")
        query = (log.get("query") or "")[:100]
        result_count = log.get("result_count", 0)
        status = log.get("status", "")
        print(f"  [{action}] {label}: q='{query}' results={result_count} status={status}")

    # Auto augmentations
    clarify_state = srr.get("_clarify_state", {})
    auto_augs = clarify_state.get("auto_augmentations", [])
    print(f"\n--- Auto Augmentations ({len(auto_augs)} entries) ---")
    for aug in auto_augs:
        field = aug.get("field", "")
        value = str(aug.get("value", ""))[:150]
        source = aug.get("source", "")
        sim = aug.get("similarity", 0)
        print(f"  field={field}, similarity={sim}, source={source}")
        print(f"    value={value}")

    # Questions
    questions = data.get("questions", []) or clarify_state.get("questions", [])
    print(f"\n--- Questions ({len(questions)} entries) ---")
    for q in questions:
        qid = q.get("id", "")
        field = q.get("field", "")
        question = (q.get("question") or q.get("description", ""))[:120]
        category = q.get("category", "")
        priority = q.get("priority", "")
        print(f"  [{priority}] {category}: {question}")
        print(f"    field={field}, id={qid}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if data.get("kb_answered", 0) > 0:
        print(f"✅ KB augmentation WORKING: {data['kb_answered']}/{data['kb_total']} questions answered by KB")
    elif data.get("kb_total", 0) > 0:
        print(f"⚠️ KB queries generated ({data['kb_total']}) but 0 answered — check search results")
    else:
        print("ℹ️ No KB queries needed (complete document or no gaps detected)")


def main():
    global TOKEN
    TOKEN = login()
    print(f"Login OK")

    # Step 1: Upload and archive first document
    req_id_1 = upload_and_archive_first()
    if not req_id_1:
        print("First document upload failed, aborting")
        return

    # Step 2: Upload second document and check KB augmentation
    upload_second_and_check_augmentation()


if __name__ == "__main__":
    main()
