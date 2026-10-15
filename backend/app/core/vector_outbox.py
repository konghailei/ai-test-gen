"""向量库写入出箱（Transactional Outbox，docs/15 §4.4）。

要解决的问题（审查证据）：
    PG 记录与 Milvus 向量分属两套系统、**不在同一事务**；Milvus 写甚至发生在 PG commit
    之前。失败时 PG 有、向量没有（"幽灵缺失"），而 TestCase 连"没写成功"都没处记录。
    审批/驳回（feedback）同样是 PG + Milvus 的非原子双写。

做法（正解：业务行与"投递意图"同事务提交，relay 至少一次投递 + 消费侧幂等）：
    ① 业务事务内 `enqueue()` 写一行 outbox（**不 commit**，与 TestCase/Traceability 一起提交）
       —— PG commit 前崩溃 → 业务行与出箱事件**一起**回滚，不会有孤儿向量意图；
    ② relay `dispatch_batch()` 认领 pending/failed 且到期的行
       （`FOR UPDATE SKIP LOCKED`，多实例安全），调 Milvus 幂等 upsert/delete；
    ③ 成功后标 done；失败记 error + 指数退避重试，超上限转 failed 等对账；
    ④ Milvus metadata 带 `revision` + `content_hash` → 对账能识别**陈旧向量**。

崩溃点覆盖（docs/15 §4.4 验收）：
    - PG commit 前崩溃：业务行与出箱一起回滚（无副作用）；
    - Milvus 成功后、relay ack 前崩溃：行仍是 pending → 下次重新 upsert **同一主键**，
      幂等，不产生重复向量；
    - relay 反复失败：退避重试 → 超上限 failed → 由 `reconcile_cases()` 重新入队。

⚠️ 与 `core/kb_vector_reconcile.py` 的分工（**避免两份删除判定漂移**）：
    - 本模块负责**写入侧**（补缺失向量，幂等 upsert）；
    - 删除侧的判定权仍在对账模块（孤儿/陈旧向量），本模块只在显式开关下投递 delete，
      默认只**报告**不执行删除 —— 误删不可逆。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text

from app.utils.logger import logger

# 投递重试上限与退避（秒）：5 → 10 → 20 → … 上限 300
OUTBOX_MAX_RETRY = 8
OUTBOX_BACKOFF_BASE = 5
OUTBOX_BACKOFF_CAP = 300
OUTBOX_BATCH = 50
OUTBOX_INTERVAL_SECONDS = 5

VALID_ACTIONS = ("upsert", "delete")
# v3.5.0（docs/18 P0）：fp_crsr = 功能点权威完整版 SRR（CRSR 编译层产物）。
# CRSR 同样遵循出箱语义：PG 固化行（functional_point_crsr）与"写入 Milvus 的意图"
# 同事务提交，relay 幂等 upsert（确定性 ID，重复投递不堆积）。
VALID_AGGREGATES = ("test_case", "requirement", "fp_crsr")


def gen_event_id() -> str:
    return f"VO-{int(time.time() * 1000)}-{secrets.token_hex(2)}"


def content_hash_of(payload: Any) -> str:
    """内容 hash（含顺序无关的键序），用于识别"向量是不是旧版本"。"""
    normalized = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def next_retry_delay(retry_count: int) -> int:
    """指数退避（纯函数，便于单测）。"""
    if retry_count <= 0:
        return 0
    return min(OUTBOX_BACKOFF_BASE * (2 ** (retry_count - 1)), OUTBOX_BACKOFF_CAP)


def enqueue(
    session,
    *,
    aggregate_id: str,
    action: str,
    payload: dict | None = None,
    aggregate_type: str = "test_case",
    revision: int = 1,
    created_by: str = "pipeline",
    content_hash: str = "",
) -> str:
    """在**调用方的事务里**追加一条出箱事件（不 commit）。

    ⚠️ 刻意不 commit：必须与业务行同一事务提交，否则又变回"两个系统各写一次"。
    """
    from app.db.models import VectorOutbox

    if action not in VALID_ACTIONS:
        raise ValueError(f"不支持的 outbox action: {action}")
    if aggregate_type not in VALID_AGGREGATES:
        raise ValueError(f"不支持的 aggregate_type: {aggregate_type}")
    event_id = gen_event_id()
    session.add(VectorOutbox(
        event_id=event_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        action=action,
        revision=int(revision),
        content_hash=content_hash or content_hash_of(payload or {}),
        payload=payload or {},
        status="pending",
        created_by=created_by,
    ))
    return event_id


def _deliver(row) -> None:
    """真正投递到 Milvus（同步，由 to_thread 调用）。幂等：按主键 upsert / delete。"""
    from app.db.milvus_store import vector_store

    payload = row.payload or {}
    if row.aggregate_type == "test_case":
        if row.action == "upsert":
            metadata = dict(payload.get("metadata") or {})
            metadata["revision"] = row.revision
            metadata["content_hash"] = row.content_hash
            vector_store.add_test_case(
                row.aggregate_id,
                payload.get("case_json") or {},
                metadata=metadata,
                product_id=metadata.get("product_id") or payload.get("product_id") or "",
            )
        else:
            vector_store.delete_test_case(row.aggregate_id)
    elif row.aggregate_type == "requirement":
        if row.action == "upsert":
            metadata = dict(payload.get("metadata") or {})
            metadata["revision"] = row.revision
            metadata["content_hash"] = row.content_hash
            vector_store.add_requirement(
                row.aggregate_id,
                payload.get("requirement_json") or {},
                metadata=metadata,
            )
        else:
            vector_store.delete_requirement(row.aggregate_id)
    elif row.aggregate_type == "fp_crsr":
        # v3.5.0（docs/18 P0-T1）：CRSR 投递。复用 add_requirement（SRR 同构，
        # _srr_to_text 同口径）；metadata.type=fp_crsr 由 payload 带入，
        # add_requirement 会自动同步 doc_type（milvus_store 内部 meta["doc_type"]=meta["type"]）。
        if row.action == "upsert":
            metadata = dict(payload.get("metadata") or {})
            metadata["revision"] = row.revision
            metadata["content_hash"] = row.content_hash
            vector_store.add_requirement(
                row.aggregate_id,
                payload.get("crsr_content") or {},
                metadata=metadata,
            )
        else:
            vector_store.delete_requirement(row.aggregate_id)
    else:
        raise ValueError(f"未知 aggregate_type: {row.aggregate_type}")


def _claim_sql(limit: int) -> str:
    return """
        SELECT event_id FROM vector_outbox
        WHERE status IN ('pending', 'failed')
          AND (next_retry_at IS NULL OR next_retry_at <= now())
        ORDER BY created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    """


async def dispatch_batch(limit: int = OUTBOX_BATCH) -> dict:
    """认领并投递一批出箱事件（至少一次；Milvus 侧幂等）。

    Returns:
        dict: {"claimed": n, "done": n, "failed": n, "errors": [...]}
    """
    from app.db.database import async_session
    from app.db.models import VectorOutbox

    stats = {"claimed": 0, "done": 0, "failed": 0, "errors": []}
    async with async_session() as session:
        rows = (await session.execute(
            text(_claim_sql(limit)), {"limit": limit}
        )).all()
        if not rows:
            return stats
        ids = [r[0] for r in rows]
        objects = (await session.execute(
            select(VectorOutbox).where(VectorOutbox.event_id.in_(ids))
        )).scalars().all()
        stats["claimed"] = len(objects)

        for row in objects:
            try:
                await asyncio.to_thread(_deliver, row)
                row.status = "done"
                row.processed_at = datetime.now(timezone.utc)
                row.error = ""
                stats["done"] += 1
            except Exception as e:  # noqa: BLE001
                row.retry_count = (row.retry_count or 0) + 1
                row.error = str(e)[:500]
                if row.retry_count >= OUTBOX_MAX_RETRY:
                    row.status = "failed"
                    row.next_retry_at = None
                    logger.error(
                        "[VectorOutbox] ⛔ 投递超上限转 failed（等对账）: event={}, agg={}, retry={}, err={}",
                        row.event_id, row.aggregate_id, row.retry_count, row.error[:160],
                    )
                else:
                    delay = next_retry_delay(row.retry_count)
                    row.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    logger.warning(
                        "[VectorOutbox] 投递失败将重试: event={}, agg={}, retry={}, 退避={}s, err={}",
                        row.event_id, row.aggregate_id, row.retry_count, delay, row.error[:160],
                    )
                stats["failed"] += 1
                stats["errors"].append({"event_id": row.event_id, "error": row.error[:200]})
        await session.commit()
    if stats["done"] or stats["failed"]:
        logger.info(
            "[VectorOutbox] 本轮投递: claimed={}, done={}, failed={}",
            stats["claimed"], stats["done"], stats["failed"],
        )
    return stats


async def pending_summary() -> dict:
    """出箱积压概览（运维/探针用）。"""
    from app.db.database import async_session
    from app.db.models import VectorOutbox

    async with async_session() as session:
        rows = (await session.execute(
            select(VectorOutbox.status, func.count()).group_by(VectorOutbox.status)
        )).all()
        oldest = (await session.execute(
            select(func.min(VectorOutbox.created_at)).where(VectorOutbox.status.in_(["pending", "failed"]))
        )).scalar()
    return {"by_status": {s: n for s, n in rows}, "oldest_unfinished": oldest.isoformat() if oldest else None}


# ---------------------------------------------------------------------------
# TestCase 侧对账（docs/15 §4.4：扩展对账到 TestCase 双向检查）
# ---------------------------------------------------------------------------

async def reconcile_cases(*, limit: int = 500, requeue_missing: bool = True,
                          apply_deletes: bool = False) -> dict:
    """TestCase 与向量库的双向对账。

    方向一（缺失）：PG 里 status='approved' 但向量库里没有 → **可直接补**
        （enqueue upsert，无损，`requeue_missing=True` 时执行）。
    方向二（孤儿/陈旧）：向量库里有、PG 里不存在或已 rejected → **默认只报告**
        （删除不可逆；开启 `apply_deletes` 才投递 delete）。

    ⚠️ 删除判定只有这一份实现；如果将来 kb_vector_reconcile 也要做 TestCase 删除，
    必须复用本函数，不要另写一套（漂移即误删）。
    """
    from sqlalchemy import select as _select

    from app.db.database import async_session
    from app.db.milvus_store import vector_store
    from app.db.models import TestCase

    report = {"missing_vectors": [], "orphan_vectors": [], "requeued": 0, "deleted": 0}

    async with async_session() as session:
        approved = (await session.execute(
            _select(TestCase.id, TestCase.product_id, TestCase.case_json, TestCase.status)
            .where(TestCase.status == "approved")
            .order_by(TestCase.created_at.desc())
            .limit(limit)
        )).all()
        known_ids = {r[0] for r in approved}
        rejected_ids = {
            r[0] for r in (await session.execute(
                _select(TestCase.id).where(TestCase.status == "rejected").limit(limit)
            )).all()
        }

        # ① 缺失向量：纯查询，不写
        # ⚠️ 用 `id in [...]` 而不是 `id == "..."`：实测 Milvus 对**含非 ASCII 的 id**
        # 等值查询会静默返回 0 条（`like`/`in` 正常），用 `==` 会把"存在"误判成"缺失"。
        def _exists(case_id: str) -> bool:
            col = vector_store._get_collection("test_cases")
            return bool(col.query(expr=f'id in ["{case_id}"]', output_fields=["id"]))

        missing = []
        for cid, product_id, case_json, _ in approved:
            try:
                if not await asyncio.to_thread(_exists, cid):
                    missing.append((cid, product_id, case_json))
            except Exception as e:  # noqa: BLE001
                logger.warning("[VectorOutbox/对账] 向量存在性查询失败（跳过）: id={}, err={}", cid, str(e)[:120])
        report["missing_vectors"] = [m[0] for m in missing]

        if missing and requeue_missing:
            for cid, product_id, case_json in missing:
                enqueue(session, aggregate_id=cid, action="upsert",
                        payload={"case_json": case_json or {}, "metadata": {"product_id": product_id or ""}},
                        created_by="reconcile")
                report["requeued"] += 1
            await session.commit()

        # ② 孤儿/陈旧：向量库有、PG 无（或已 rejected）→ 默认只报告
        def _list_vector_ids() -> set[str]:
            base = vector_store._get_collection("test_cases")
            base.load()
            # ⚠️ doc_type / product_id 等都在 `metadata` 这个 JSON 字段里，
            # 不是顶层字段 → 必须用 JSON 路径表达式过滤（`doc_type == ...` 会报
            # "field doc_type not exist"）。
            sample = base.query(expr='metadata["doc_type"] == "test_case"',
                                output_fields=["id"], limit=limit)
            return {r["id"] for r in sample}

        # v4.0.1（S1 从根治理）：集合加载 + query 是**同步** Milvus 链路，
        # 原实现在事件循环里直调会阻塞所有请求；整段交给线程执行。
        try:
            vector_ids = await asyncio.to_thread(_list_vector_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("[VectorOutbox/对账] 向量清单查询失败（跳过孤儿检查）: {}", str(e)[:120])
            vector_ids = set()

        orphan = sorted(vector_ids - known_ids - rejected_ids)
        report["orphan_vectors"] = orphan
        if orphan and apply_deletes:
            for cid in orphan:
                enqueue(session, aggregate_id=cid, action="delete", payload={}, created_by="reconcile")
                report["deleted"] += 1
            await session.commit()
        elif orphan:
            logger.warning(
                "[VectorOutbox/对账] 发现 {} 条疑似孤儿/陈旧向量（默认不删，需显式 apply_deletes）: {}",
                len(orphan), orphan[:5],
            )
    return report


# ---------------------------------------------------------------------------
# Relay（应用内 asyncio 循环；本项目没有 Celery beat，周期任务都走 lifespan）
# ---------------------------------------------------------------------------

_relay_task: asyncio.Task | None = None


async def _relay_loop(interval: int) -> None:
    logger.info("[VectorOutbox] relay 已启动: interval={}s, batch={}", interval, OUTBOX_BATCH)
    while True:
        try:
            await dispatch_batch()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("[VectorOutbox] relay 轮次异常（不退出循环）: {}", str(e)[:200])
        await asyncio.sleep(interval)


def start_vector_outbox_relay() -> asyncio.Task | None:
    global _relay_task
    if _relay_task is not None and not _relay_task.done():
        return _relay_task
    _relay_task = asyncio.create_task(_relay_loop(OUTBOX_INTERVAL_SECONDS))
    return _relay_task


async def stop_vector_outbox_relay() -> None:
    global _relay_task
    if _relay_task is None:
        return
    _relay_task.cancel()
    try:
        await _relay_task
    except asyncio.CancelledError:
        pass
    _relay_task = None
    logger.info("[VectorOutbox] relay 已停止")
