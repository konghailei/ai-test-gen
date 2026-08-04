# -*- coding: utf-8 -*-
"""Update all 6 _save_fps_to_db call sites to pass product_id (Bug A-1)."""
import sys

p = 'app/api/requirements.py'
with open(p, 'r', encoding='utf-8') as f:
    src = f.read()

replacements = [
    # call sites inside _run_requirement_analysis (product_id is a param)
    (
        'await _save_fps_to_db(save_s, req_id, captured_fps, req_data.get("priority", "P2"))',
        'await _save_fps_to_db(save_s, req_id, captured_fps, req_data.get("priority", "P2"), product_id=product_id)',
    ),
    (
        'await _save_fps_to_db(s, req_id, captured_fps, req_data.get("priority", "P2"))',
        'await _save_fps_to_db(s, req_id, captured_fps, req_data.get("priority", "P2"), product_id=product_id)',
    ),
    # inside _run_clarify_reparse (product_id is a param)
    (
        'await _save_fps_to_db(s, req_id, captured_fps, priority)',
        'await _save_fps_to_db(s, req_id, captured_fps, priority, product_id=product_id)',
    ),
    # inside finalize_requirement (record.product_id available)
    (
        'await _save_fps_to_db(session, req_id, finalize_result.fps, record.priority)',
        'await _save_fps_to_db(session, req_id, finalize_result.fps, record.priority, product_id=record.product_id or "")',
    ),
    # inside _run_regenerate_reparse (product_id is a param) - two call sites
    (
        'await _save_fps_to_db(save_s, req_id, captured_fps, req_row or "P2")',
        'await _save_fps_to_db(save_s, req_id, captured_fps, req_row or "P2", product_id=product_id)',
    ),
    (
        'await _save_fps_to_db(fp_split_sess, req_id, captured_fps, record2.priority)',
        'await _save_fps_to_db(fp_split_sess, req_id, captured_fps, record2.priority, product_id=product_id)',
    ),
]

for old, new in replacements:
    cnt = src.count(old)
    if cnt != 1:
        print(f"ERROR: expected 1 occurrence of: {old[:80]}...; found {cnt}")
        sys.exit(2)
    src = src.replace(old, new)
    print(f"OK: {new[:90]}...")

with open(p, 'w', encoding='utf-8') as f:
    f.write(src)
print("All 6 call sites updated.")
