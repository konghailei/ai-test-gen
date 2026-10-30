"""向量出箱的离线用例（纯函数部分）。

⚠️ 刻意**不 import `app.db.models` / `app.db.database`**：这两个模块在导入时就要求
`DATABASE_URL`，一旦引入，CI 的 unit 层（无外部服务）会直接收集失败。
需要真库/真 Milvus 的行为验证放在 `backend/_probe_outbox.py`（容器内跑）。
"""
import pytest

from app.core.vector_outbox import (
    OUTBOX_BACKOFF_CAP,
    VALID_AGGREGATES,
    content_hash_of,
    next_retry_delay,
)


def test_content_hash_is_order_insensitive_and_stable():
    a = {"b": 2, "a": 1, "nested": {"y": [1, 2]}}
    b = {"nested": {"y": [1, 2]}, "a": 1, "b": 2}
    assert content_hash_of(a) == content_hash_of(b)
    assert len(content_hash_of(a)) == 64


def test_content_hash_changes_with_content():
    assert content_hash_of({"title": "A"}) != content_hash_of({"title": "B"})
    # 内容变化就要检测出来（用于识别"向量是不是旧版本"）
    assert content_hash_of({"title": "A", "steps": [1]}) != content_hash_of({"title": "A"})


def test_content_hash_handles_empty():
    assert content_hash_of(None) == content_hash_of({})


@pytest.mark.parametrize("retry,expected", [
    (0, 0), (1, 5), (2, 10), (3, 20), (4, 40), (5, 80), (6, 160), (7, 300), (8, 300),
])
def test_backoff_is_exponential_and_capped(retry, expected):
    assert next_retry_delay(retry) == expected


def test_backoff_never_exceeds_cap():
    for r in range(1, 30):
        assert next_retry_delay(r) <= OUTBOX_BACKOFF_CAP


def test_backoff_is_monotonic():
    seq = [next_retry_delay(r) for r in range(1, 10)]
    assert seq == sorted(seq)


# ---------------------------------------------------------------------------
# v3.5.0（docs/18 P0-T1）：fp_crsr 聚合类型守卫
# ---------------------------------------------------------------------------

def test_fp_crsr_is_valid_aggregate():
    """T1 守卫：fp_crsr 必须在合法聚合类型里——否则 enqueue 直接抛 ValueError，
    CRSR 永远进不了向量库（静默失效）。"""
    assert "fp_crsr" in VALID_AGGREGATES


def test_crsr_vector_id_is_deterministic_and_distinct():
    """确定性 ID：同 (product, fp_name) 跨调用一致（幂等 upsert 的前提）；
    不同 product / 不同 fp_name 必须不同（防串写）。"""
    from app.core.fp_crsr import crsr_vector_id

    a1 = crsr_vector_id("PROD-1", "登录")
    a2 = crsr_vector_id("PROD-1", "登录")
    assert a1 == a2
    assert a1.startswith("CRSR-")
    assert crsr_vector_id("PROD-2", "登录") != a1
    assert crsr_vector_id("PROD-1", "登出") != a1
    # 顺序敏感（防止 (A|B) 与 (B|A) 哈希碰撞的隐忧）
    assert crsr_vector_id("PROD-1|X", "") != crsr_vector_id("", "PROD-1|X")


def test_crsr_outbox_payload_carries_required_metadata():
    """T5 守卫：payload metadata 必含检索硬过滤所需字段。

    尤其 status="archived"——search_similar_requirements 按 status=archived 硬过滤，
    漏了 = 向量存在但永远检索不到（最隐蔽的静默失效形态）。
    """
    from app.core.fp_crsr import crsr_outbox_payload
    from app.db.models import FunctionalPointCRSR
    from datetime import datetime, timezone

    row = FunctionalPointCRSR(
        product_id="PROD-X",
        fp_name="登录",
        content={"summary": "s", "business_rules": [{"id": "BR-1", "rule": "r"}]},
        source_fp_ids=["FP-1", "FP-2"],
        version_refs=[{"version_id": "V1.0-演示"}],
    )
    payload = crsr_outbox_payload(row)
    md = payload["metadata"]
    assert payload["crsr_content"] == row.content
    assert md["type"] == "fp_crsr"
    assert md["status"] == "archived"          # 硬过滤必含
    assert md["product_id"] == "PROD-X"
    assert md["fp_name"] == "登录"
    assert md["title"] == "登录"
    assert md["version_count"] == 2
    assert md["latest_version_ref"] == "V1.0-演示"
    assert md["consolidated_at"]               # ISO 时间串


def test_crsr_deliver_routes_to_add_requirement(monkeypatch):
    """_deliver 的 fp_crsr 分支必须路由到 add_requirement（SRR 同构复用），
    且 payload 缺 crsr_content 时也不炸（空 dict 兜底，与 requirement 分支同语义）。"""
    from app.core import vector_outbox as vo

    calls = []

    class _FakeStore:
        def add_requirement(self, req_id, srr_json, metadata=None):
            calls.append(("upsert", req_id, srr_json, metadata))

        def delete_requirement(self, req_id, product_id=None):
            # v4.1.9：删除分支支持透传 product_id 以失效该产品 KB 缓存
            calls.append(("delete", req_id, product_id))

    monkeypatch.setattr(
        "app.db.milvus_store.vector_store", _FakeStore()
    )

    row = type("_R", (), {
        "aggregate_type": "fp_crsr",
        "aggregate_id": "CRSR-abc",
        "action": "upsert",
        "revision": 123,
        "content_hash": "h" * 64,
        "payload": {
            "crsr_content": {"summary": "s"},
            "metadata": {"type": "fp_crsr", "status": "archived", "product_id": "P"},
        },
    })()
    vo._deliver(row)
    assert calls and calls[0][0] == "upsert" and calls[0][1] == "CRSR-abc"
    assert calls[0][3]["revision"] == 123      # revision 透传（陈旧向量识别依赖）

    calls.clear()
    row_del = type("_R", (), {
        "aggregate_type": "fp_crsr", "aggregate_id": "CRSR-abc",
        "action": "delete", "revision": 1, "content_hash": "",
        "payload": {},
    })()
    vo._deliver(row_del)
    assert calls and calls[0][0] == "delete"
    # payload 为空 → 不失效任何产品缓存
    assert calls[0][2] is None

    # v4.1.9：payload 带 product_id 时必须透传，否则「退出知识库」后
    # 该产品的 KB 检索缓存在 24h TTL 内仍会命中已删向量
    calls.clear()
    row_del_pid = type("_R", (), {
        "aggregate_type": "requirement", "aggregate_id": "REQ-x",
        "action": "delete", "revision": 1, "content_hash": "",
        "payload": {"product_id": "PROD-1"},
    })()
    vo._deliver(row_del_pid)
    assert calls and calls[0][0] == "delete"
    assert calls[0][2] == "PROD-1"


def test_kqa_type_mapping_recognizes_fp_crsr():
    """T2 守卫（N-6 同款防线）：kqa 类型映射必须显式识别 fp_crsr——
    若落到 else 会被误标 requirement → _enrich_sources 查不到 → 孤儿丢弃。
    断言源码包含显式分支（离线可测；行为级验证在容器内端到端）。"""
    import inspect
    from app.api import knowledge_qa as kqa

    src = inspect.getsource(kqa)
    # 显式 fp_crsr 分支存在（elif _md_type == "fp_crsr"）
    assert 'elif _md_type == "fp_crsr"' in src
    # 灰度开关消费方存在
    assert "KB_CRSR_ENABLED" in src
    # 权威层级注册（否则富化时 fallback 成 L1 之外的默认值——目前默认恰是 L1，
    # 此断言防未来默认值变更时静默漂移）
    assert '"fp_crsr": "L1"' in src


def test_enrich_sources_has_crsr_branch():
    """T3 守卫：_enrich_sources 必须有 fp_crsr 分支（否则 CRSR 条目被当孤儿丢弃）。"""
    import inspect
    from app.api import knowledge_qa as kqa

    src = inspect.getsource(kqa._enrich_sources)
    assert 'src_type == "fp_crsr"' in src
    # 孤儿过滤条件行覆盖 fp_crsr（固化行删除后的向量要能被识别为孤儿）
    # —— 直接断言条件行原文，不做脆弱的窗口截取
    assert 'src_type in ("requirement", "functional_point", "fp_crsr")' in src


# ---------------------------------------------------------------------------
# v4.1.9：scope（范围删除）分支
# ---------------------------------------------------------------------------

def _scope_row(payload: dict, action: str = "delete"):
    return type("_R", (), {
        "event_id": "VO-SCOPE-1",
        "aggregate_type": "scope",
        "aggregate_id": f"{payload.get('scope_type')}:{payload.get('scope_id')}",
        "action": action,
        "revision": 0,
        "content_hash": "",
        "payload": payload,
    })()


def test_scope_product_routes_to_batch_deletes(monkeypatch):
    """删产品：按 product_id 批量删需求/用例向量（FP 向量也带 product_id，一并覆盖）。"""
    from app.core import vector_outbox as vo

    calls = []

    class _Store:
        def delete_requirement_by_product(self, pid):
            calls.append(("req_by_product", pid))
            return 3

        def delete_test_case_by_product(self, pid):
            calls.append(("case_by_product", pid))
            return 5

    monkeypatch.setattr("app.db.milvus_store.vector_store", _Store())
    vo._deliver(_scope_row({"scope_type": "product", "scope_id": "P1", "product_id": "P1"}))

    assert ("req_by_product", "P1") in calls
    assert ("case_by_product", "P1") in calls


def test_scope_project_also_deletes_fp_vectors(monkeypatch):
    """删项目：by_project 删不掉 FP 向量（FP CRSR metadata 无 project_id），
    必须按 payload.fp_ids 显式补删——漏掉就是永久孤儿。"""
    from app.core import vector_outbox as vo

    calls = []

    class _Store:
        def delete_requirement_by_project(self, pid):
            calls.append(("req_by_project", pid))
            return 1

        def delete_test_case_by_project(self, pid):
            calls.append(("case_by_project", pid))
            return 2

        def delete_requirement(self, req_id, product_id=None):
            calls.append(("req", req_id, product_id))

    monkeypatch.setattr("app.db.milvus_store.vector_store", _Store())
    monkeypatch.setattr(
        "app.db.kb_cache.kb_cache.invalidate_by_product_sync",
        lambda pid: calls.append(("cache", pid)),
    )

    vo._deliver(_scope_row({
        "scope_type": "project", "scope_id": "PJ1",
        "product_id": "P1", "fp_ids": ["FP1", "FP2"],
    }))

    assert ("req_by_project", "PJ1") in calls
    assert ("case_by_project", "PJ1") in calls
    assert ("req", "FP-FP1", None) in calls
    assert ("req", "FP-FP2", None) in calls
    # by_project 内部不失效缓存，必须显式失效
    assert ("cache", "P1") in calls


def test_scope_req_deletes_cases_only(monkeypatch):
    """按需求批量删用例：只删 test_cases 集合，不碰需求向量。"""
    from app.core import vector_outbox as vo

    calls = []

    class _Store:
        def delete_cases_by_req_id(self, rid):
            calls.append(("cases_by_req", rid))
            return 4

    monkeypatch.setattr("app.db.milvus_store.vector_store", _Store())
    vo._deliver(_scope_row({"scope_type": "req", "scope_id": "REQ-1"}))

    assert calls == [("cases_by_req", "REQ-1")]


def test_scope_rejects_upsert(monkeypatch):
    """scope 只表达删除；写成 upsert 必须显式报错，不能静默当删除执行。"""
    from app.core import vector_outbox as vo

    monkeypatch.setattr("app.db.milvus_store.vector_store", object())

    try:
        vo._deliver(_scope_row({"scope_type": "product", "scope_id": "P1"}, action="upsert"))
    except ValueError as e:
        assert "delete" in str(e)
    else:  # pragma: no cover
        raise AssertionError("scope + upsert 应抛 ValueError")
