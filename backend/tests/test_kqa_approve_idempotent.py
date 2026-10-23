"""P-4 修复守卫：kqa approve / archive 的幂等与出箱语义（v4.1.3）。

背景（docs/KNOWN-ISSUES.md P-4）：
    原 `kqa_approve_correction` 是「先写 Milvus、后靠请求尾 commit 落 PG 状态」：
      - 两系统非原子：Milvus 已写、PG 回滚 → 记录仍是 pending_review，
        重试即**重复归档**（重复 upsert + 重复 CRSR 刷新）；
      - 无行锁：并发双击两个请求都能读到 pending_review → 双写。
    现改为「`FOR UPDATE` 行锁 + 状态机幂等 + 全部变更（含向量投递意图）同一事务提交」。

这里用**行为级 fake session**（记录 SQL/WAL，不连真 PG），断言：
    1. 已 approved 再 approve → 幂等返回、零副作用（不添加需求、不入箱、不重扫 FP）；
    2. pending 首次 approve → 业务行 + 出箱事件同批提交（一次 commit，无中间 commit）；
    3. 向量写入**不再**发生在请求内（不走 vector_store.add_requirement / delete_requirement）；
    4. 独立片段归档分支也会入箱（否则 PG 提交后向量永远缺失）。
落库真实性与并发行为在容器内端到端脚本另验（见 docs 的 P-4 修复记录）。
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# 最小 fake session：够用即可，不做 SQL 解析（只记录"发生过什么"）
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value or []


class _FakeSession:
    """记录 commit / rollback / add / SQL 语句的 fake AsyncSession。"""

    def __init__(self, *, record=None, candidate=None, fps=None):
        self.record = record            # KnowledgeQA 记录
        self.candidate = candidate      # correction_target_id 指向的 Requirement
        self.fps = fps or []
        self.added: list = []
        self.commit_count = 0
        self.rollback_count = 0
        self.statements: list = []

    async def execute(self, stmt, *a, **kw):
        self.statements.append(str(stmt))
        text = str(stmt)
        # 判定这是哪类查询：看 where 条件里的表名，不解析具体值
        if "knowledge_qa" in text:
            return _Result(self.record)
        if "FROM requirements" in text:
            return _Result(self.candidate)
        if "functional_points" in text:
            return _Result(self.fps)
        return _Result(None)

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, pk):
        # FP 分支用它取 FunctionalPoint 行
        return self.fps[0] if self.fps else None

    async def flush(self):
        pass

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _make_record(**over):
    base = dict(
        id="KQA-20260101-TESTTEST",
        question="上传失败怎么处理？",
        answer="原回答",
        corrected_answer="纠正后的回答内容",
        correction_notes="",
        correction_status="pending_review",
        correction_target_type="requirement",
        correction_target_id="",
        correction_module="",
        product_id="PROD-TEST",
        archived=False,
        archived_type="",
        archived_id="",
        reviewed_by="",
        reviewed_at=None,
        review_notes="",
        updated_at=datetime.now(timezone.utc),
        answer_status="ok",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _async(coro):
    """单进程单次 asyncio.run（避免多次 run 触发 Event loop is closed）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. 幂等：已 approved 再 approve，零副作用
# ---------------------------------------------------------------------------

def test_approve_is_idempotent_when_already_approved(monkeypatch):
    """已审核通过 → 直接回放上次结果；不得新增需求、不得入箱、不得重扫 FP。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(
        correction_status="approved",
        archived=True,
        archived_type="requirement",
        archived_id="REQ-EXISTING-001",
    )
    fake = _FakeSession(record=record)

    enqueued: list = []
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: enqueued.append(kw) or "VO-fake",
    )

    resp = _async(kqa.kqa_approve_correction(
        "KQA-20260101-TESTTEST",
        SimpleNamespace(review_notes="dup"),
        session=fake,
        current_user=SimpleNamespace(username="tester"),
    ))

    assert resp.success is True
    assert resp.data["idempotent"] is True
    assert resp.data["archived_id"] == "REQ-EXISTING-001"
    # 零副作用
    assert enqueued == [], "重复 approve 不应产生新的出箱事件"
    assert fake.added == [], "重复 approve 不应新增业务行"
    assert fake.commit_count == 0, "幂等短路不应写库（无 commit）"


# ---------------------------------------------------------------------------
# 2. 首次 approve：一行出箱 + 一次提交（无中间 commit）
# ---------------------------------------------------------------------------

def test_approve_first_time_enqueues_and_commits_once(monkeypatch):
    """pending → approved：向量走 outbox，业务行与出箱同一次 commit。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(correction_target_type="requirement", correction_target_id="")
    fake = _FakeSession(record=record)

    enqueued: list = []
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: enqueued.append(kw) or "VO-fake",
    )

    resp = _async(kqa.kqa_approve_correction(
        "KQA-20260101-TESTTEST",
        SimpleNamespace(review_notes="ok"),
        session=fake,
        current_user=SimpleNamespace(username="tester"),
    ))

    assert resp.success is True
    assert resp.data["vector_delivery"] == "outbox"
    assert record.correction_status == "approved"
    assert record.archived is True
    assert record.archived_id.startswith("REQ-")

    # 恰好一条出箱事件，类型/动作正确
    assert len(enqueued) == 1
    assert enqueued[0]["aggregate_type"] == "requirement"
    assert enqueued[0]["action"] == "upsert"
    assert enqueued[0]["aggregate_id"] == record.archived_id
    # 出箱 payload 必须自带 SRR 与 metadata（relay 侧不查 PG）
    assert "requirement_json" in enqueued[0]["payload"]
    assert enqueued[0]["payload"]["metadata"]["qa_id"] == "KQA-20260101-TESTTEST"

    # 只 commit 一次（原实现在 CRSR 处还会插一次 commit → 中间态）
    assert fake.commit_count == 1, f"应恰好一次提交，实际 {fake.commit_count}"
    assert fake.rollback_count == 0


# ---------------------------------------------------------------------------
# 3. 向量写入不再发生在请求内
# ---------------------------------------------------------------------------

def test_approve_does_not_touch_vector_store_inline(monkeypatch):
    """守卫：approve 路径内不得再直调 vector_store 写/删（P-4 的根就在"先写向量"）。"""
    from app.api import knowledge_qa as kqa
    import app.db.milvus_store as ms

    calls: list = []
    real_store = ms.vector_store

    class _Spy:
        def __getattr__(self, name):
            def _f(*a, **kw):
                calls.append(name)
                raise AssertionError(f"approve 不应在请求内调用 vector_store.{name}")
            return _f

    monkeypatch.setattr(ms, "vector_store", None)  # 触发误用即炸
    record = _make_record(correction_target_type="requirement", correction_target_id="")
    fake = _FakeSession(record=record)
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: "VO-fake",
    )

    try:
        resp = _async(kqa.kqa_approve_correction(
            "KQA-20260101-TESTTEST",
            SimpleNamespace(review_notes="ok"),
            session=fake,
            current_user=SimpleNamespace(username="tester"),
        ))
    finally:
        monkeypatch.setattr(ms, "vector_store", real_store)

    assert resp.success is True
    assert calls == []


# ---------------------------------------------------------------------------
# 4. FP 分支：同 key upsert（不再 delete-then-add）
# ---------------------------------------------------------------------------

def test_approve_fp_branch_uses_upsert_not_delete(monkeypatch):
    """FP 级纠正：只投递一条 upsert（同 fp_id 覆盖），不再先 delete 再 add。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(
        correction_target_type="functional_point",
        correction_target_id="FP-001",
    )
    fake = _FakeSession(record=record, fps=[])

    enqueued: list = []
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: enqueued.append(kw) or "VO-fake",
    )

    resp = _async(kqa.kqa_approve_correction(
        "KQA-20260101-TESTTEST",
        SimpleNamespace(review_notes="ok"),
        session=fake,
        current_user=SimpleNamespace(username="tester"),
    ))

    assert resp.data["archived_type"] == "functional_point"
    assert resp.data["archived_id"] == "FP-001"
    assert len(enqueued) == 1
    assert enqueued[0]["action"] == "upsert"      # 不是 delete
    assert enqueued[0]["aggregate_id"] == "FP-001"
    assert enqueued[0]["payload"]["metadata"]["type"] == "functional_point"


# ---------------------------------------------------------------------------
# 5. 需求级原位合并：payload 带的是"改后"的 SRR
# ---------------------------------------------------------------------------

def test_approve_inplace_merge_enqueues_updated_srr(monkeypatch):
    """原位合并：出箱 payload 的 summary 必须已含纠正补充（否则重嵌仍是旧内容）。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(correction_target_id="REQ-SRC-001")
    target = SimpleNamespace(
        id="REQ-SRC-001",
        product_id="PROD-TEST",
        project_id="PRJ-1",
        status="archived",
        title="来源需求",
        raw_text="原始需求正文",
        srr_json={"requirement": {"title": "来源需求", "summary": "旧摘要"}},
        updated_at=datetime.now(timezone.utc),
    )
    fake = _FakeSession(record=record, candidate=target, fps=[])

    enqueued: list = []
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: enqueued.append(kw) or "VO-fake",
    )

    resp = _async(kqa.kqa_approve_correction(
        "KQA-20260101-TESTTEST",
        SimpleNamespace(review_notes="ok"),
        session=fake,
        current_user=SimpleNamespace(username="tester"),
    ))

    assert resp.data["archived_id"] == "REQ-SRC-001"
    assert len(enqueued) == 1
    srr = enqueued[0]["payload"]["requirement_json"]
    # 改后的 SRR 里必须含纠正内容（顺带证明不是把旧 srr_json 原样投递）
    assert "纠正后的回答内容" in srr["requirement"]["summary"]
    assert "纠正后的回答内容" in target.raw_text
    # 原位合并标记
    assert "已原位合并至来源需求" in record.correction_notes


# ---------------------------------------------------------------------------
# 6. 行锁：approve 与 reject 都必须先加 FOR UPDATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("func_name", ["kqa_approve_correction", "kqa_reject_correction"])
def test_review_endpoints_take_row_lock(func_name):
    """守卫：两个审核入口都必须 `with_for_update()`（原实现裸 select，并发双击双写）。"""
    import inspect
    from app.api import knowledge_qa as kqa

    src = inspect.getsource(getattr(kqa, func_name))
    assert "with_for_update()" in src, f"{func_name} 缺少行锁：并发重复提交会双写"


def test_reject_is_idempotent_when_already_rejected(monkeypatch):
    """已 rejected 再 reject → 幂等返回，不覆盖原审核信息。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(
        correction_status="rejected",
        reviewed_by="first_reviewer",
        review_notes="原始意见",
    )
    fake = _FakeSession(record=record)

    resp = _async(kqa.kqa_reject_correction(
        "KQA-20260101-TESTTEST",
        SimpleNamespace(review_notes="第二次的意见"),
        session=fake,
        current_user=SimpleNamespace(username="second"),
    ))

    assert resp.data["idempotent"] is True
    assert record.reviewed_by == "first_reviewer"   # 不被覆盖
    assert record.review_notes == "原始意见"
    assert fake.commit_count == 0


# ---------------------------------------------------------------------------
# 7. archive：两个归档分支都要入箱
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("archive_type,expected_agg", [
    ("requirement", "requirement"),
    ("test_case", "test_case"),
])
def test_archive_enqueues_outbox(monkeypatch, archive_type, expected_agg):
    """守卫：/archive 原先是"先 send_task/写向量、后靠尾 commit 落 PG"，
    改为同事务入箱后，两种归档类型都必须真的入箱（否则 commit 后向量永远缺失）。"""
    from app.api import knowledge_qa as kqa

    record = _make_record(correction_status="none")
    fake = _FakeSession(record=record)

    enqueued: list = []
    monkeypatch.setattr(
        "app.core.vector_outbox.enqueue",
        lambda *a, **kw: enqueued.append(kw) or "VO-fake",
    )

    async def _noop_visible(*a, **kw):
        return True

    monkeypatch.setattr(kqa, "assert_product_visible", _noop_visible)

    resp = _async(kqa.kqa_archive(
        SimpleNamespace(
            id="KQA-20260101-TESTTEST",
            content="归档内容",
            title="",
            archive_type=archive_type,
        ),
        user=SimpleNamespace(username="tester"),
        session=fake,
    ))

    assert resp.success is True
    assert len(enqueued) == 1
    assert enqueued[0]["aggregate_type"] == expected_agg
    assert enqueued[0]["action"] == "upsert"
    assert enqueued[0]["aggregate_id"] == resp.data["archived_id"]
    assert fake.commit_count == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
