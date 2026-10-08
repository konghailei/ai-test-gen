# -*- coding: utf-8 -*-
"""D-6 守卫：孤儿向量扫描必须认识 functional_point_crsr 表（docs/21 §5 D-6）。

行为验证（非 grep 字面量）：CRSR 固化行对应的向量 ID 扫描后不得出现在孤儿清单。
"""
import pytest
from unittest.mock import patch

from app.core.kb_vector_reconcile import scan_orphan_vectors


class _FakeColl:
    """模拟 Milvus collection：一个 CRSR 向量 + 一个真孤儿。"""

    def __init__(self, records):
        self._records = records

    def query(self, expr="", output_fields=None, limit=None, offset=None):
        if offset and offset > 0:
            return []
        return self._records


class _FakeSession:
    """按 SQL 关键词路由各表 ID 集（行为级 fake，不依赖真 DB）。

    CRSR 特殊处理：向量 ID 由 (product_id, fp_name) 派生（crsr_vector_id），
    表里存的是 (product_id, fp_name) 行——fake 按同源算法算出合法向量 ID 集。
    """

    def __init__(self, table_ids: dict[str, list[str]],
                 crsr_rows: list[tuple[str, str]] | None = None):
        self._table_ids = table_ids
        self._crsr_rows = crsr_rows or []

    async def execute(self, q):
        sql_text = str(q).lower()
        r = _FakeResult()
        if "functional_point_crsr" in sql_text and "select product_id, fp_name" in sql_text:
            r._rows = self._crsr_rows
            r._as_rows = True
            return r
        for key, ids in self._table_ids.items():
            if key in sql_text:
                r._ids = ids
                break
        return r

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeResult:
    def __init__(self):
        self._ids: list = []
        self._rows: list = []
        self._as_rows = False

    def scalars(self):
        return self

    def all(self):
        return self._ids

    def fetchall(self):
        return self._rows


def _mk_rec(vid, vtype):
    return {"id": vid, "metadata": {"type": vtype}}


@pytest.mark.asyncio
async def test_crsr_vector_not_orphaned():
    """CRSR-xxx 向量只要 functional_point_crsr 表有对应 (product_id, fp_name) 行
    （派生算法同源），就不判孤儿。"""
    records = [
        _mk_rec("CRSR-10fd8f36d5c6fee2", "fp_crsr"),   # 派生自下面这行
        _mk_rec("FP-123", "functional_point"),
        _mk_rec("TC-XYZ", "test_case"),
        _mk_rec("GHOST-999", "knowledge_qa"),          # 表里没有 → 真孤儿
    ]

    session = _FakeSession({
        "from functional_points": ["FP-123"],
        "from test_cases": ["TC-XYZ"],
    }, crsr_rows=[("PROD-75005-135", "测试用例生成")])

    with patch("app.core.kb_vector_reconcile.async_session", return_value=session), \
         patch("app.db.milvus_store.vector_store") as mock_vs:
        mock_vs._get_collection.return_value = _FakeColl(records)
        result = await scan_orphan_vectors("requirements")

    ids = result["orphan_ids"]
    assert "CRSR-10fd8f36d5c6fee2" not in ids, "CRSR 固化行存在却被判孤儿 → D-6 回归"
    assert "GHOST-999" in ids, "真实孤儿应被报告"
    assert "FP-123" not in ids and "TC-XYZ" not in ids


@pytest.mark.asyncio
async def test_crsr_vector_orphaned_when_row_missing():
    """反向：CRSR 向量在 functional_point_crsr 表无固化行 → 仍应报孤儿（对账语义不放松）。"""
    records = [_mk_rec("CRSR-deadbeef", "fp_crsr")]
    session = _FakeSession({}, crsr_rows=[])

    with patch("app.core.kb_vector_reconcile.async_session", return_value=session), \
         patch("app.db.milvus_store.vector_store") as mock_vs:
        mock_vs._get_collection.return_value = _FakeColl(records)
        result = await scan_orphan_vectors("requirements")

    assert "CRSR-deadbeef" in result["orphan_ids"], "无固化行的 CRSR 向量应报孤儿"
