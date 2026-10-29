# -*- coding: utf-8 -*-
"""出箱状态回写解析器单测（v4.1.9）。

只测纯函数 `status_writeback_req_id`：显式声明才回写、delete/非 requirement 类型不回写。
真正的落库回写由 `verify_outbox_writeback.py` 做**真实 PG e2e** 验证
（项目纪律：mock 只证明"调用了"，落库类必须用真库证明"生效了"）。
"""
from types import SimpleNamespace

from app.core.vector_outbox import status_writeback_req_id


def _row(**kw):
    base = {"action": "upsert", "aggregate_type": "requirement", "payload": {}}
    base.update(kw)
    return SimpleNamespace(**base)


def test_returns_req_id_when_declared():
    row = _row(payload={"status_writeback": {"req_id": "REQ-1"}})
    assert status_writeback_req_id(row) == "REQ-1"


def test_none_when_not_declared():
    """correction 流（kqa_archive）等未声明的事件不得被回写状态。"""
    assert status_writeback_req_id(_row(payload={"metadata": {"type": "requirement"}})) is None


def test_none_for_delete_action():
    row = _row(action="delete", payload={"status_writeback": {"req_id": "REQ-1"}})
    assert status_writeback_req_id(row) is None


def test_none_for_non_requirement_aggregate():
    row = _row(aggregate_type="test_case", payload={"status_writeback": {"req_id": "REQ-1"}})
    assert status_writeback_req_id(row) is None


def test_none_for_malformed_payload():
    assert status_writeback_req_id(_row(payload={"status_writeback": "REQ-1"})) is None
    assert status_writeback_req_id(_row(payload=None)) is None
    assert status_writeback_req_id(_row(payload={"status_writeback": {}})) is None
