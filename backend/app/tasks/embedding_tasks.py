"""嵌入计算 + Milvus 写入异步任务。

由归档端点触发：PG commit → celery_app.send_task → Milvus 写入
失败重试 3 次，最终失败记录到日志（不阻断归档）。
成功/失败时回写 vectorization_status 到 PostgreSQL，供前端展示状态。

设计依据：
- docs/02-knowledge-base.md §9.7（异步双写一致性方案）
"""
from __future__ import annotations

import asyncio

from app.tasks.celery_app import celery_app
from app.utils.logger import logger


async def _update_vectorization_status(req_id: str, status: str, error: str | None = None):
    """异步回写需求向量化状态到 PostgreSQL。"""
    from app.db.database import async_session
    from app.db.models import Requirement
    from sqlalchemy import select

    async with async_session() as session:
        r = await session.execute(select(Requirement).where(Requirement.id == req_id))
        record = r.scalar_one_or_none()
        if record:
            record.vectorization_status = status
            record.vectorization_error = error
            await session.commit()


async def _update_tc_vectorization_status(case_id: str, status: str, error: str | None = None):
    """异步回写测试用例向量化状态到 PostgreSQL（修复 C4）。"""
    from app.db.database import async_session
    from app.db.models import TestCase
    from sqlalchemy import select

    async with async_session() as session:
        r = await session.execute(select(TestCase).where(TestCase.id == case_id))
        record = r.scalar_one_or_none()
        if record:
            record.vectorization_status = status
            record.vectorization_error = error
            await session.commit()


def _update_vs_sync(req_id: str, status: str, error: str | None = None):
    """同步封装：Celery 任务中调用 asyncio.run 来更新 DB（需求）。"""
    try:
        asyncio.run(_update_vectorization_status(req_id, status, error))
    except Exception as e:
        logger.warning(
            "[Celery] 回写需求向量化状态失败: req_id={}, status={}, error={}",
            req_id, status, str(e)[:200],
        )


def _update_tc_vs_sync(case_id: str, status: str, error: str | None = None):
    """同步封装：Celery 任务中调用 asyncio.run 来更新 DB（测试用例）。"""
    try:
        asyncio.run(_update_tc_vectorization_status(case_id, status, error))
    except Exception as e:
        logger.warning(
            "[Celery] 回写测试用例向量化状态失败: case_id={}, status={}, error={}",
            case_id, status, str(e)[:200],
        )


if celery_app is not None:

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_requirement",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
        # 修复 C4：任务级 time_limit（覆盖全局的兜底，需求可略长于 TC）
        time_limit=240,
        soft_time_limit=210,
    )
    def embed_and_store_requirement(self, req_id: str, srr_json: dict, metadata: dict):
        """异步计算嵌入并写入 Milvus（需求）。

        Args:
            req_id: 需求 ID
            srr_json: SRR JSON
            metadata: Milvus 元数据
        """
        try:
            from app.db.milvus_store import vector_store
            vector_store.add_requirement(req_id, srr_json, metadata=metadata)
            logger.info("[Celery] 嵌入+Milvus写入成功: req_id={}", req_id)
            _update_vs_sync(req_id, "completed")
        except Exception as exc:
            logger.error(
                "[Celery] 嵌入+Milvus写入失败: req_id={}, error={}",
                req_id, str(exc)[:200],
            )
            if self.request.retries < self.max_retries:
                _update_vs_sync(req_id, "pending_retry", str(exc)[:500])
                raise self.retry(exc=exc)
            # 全部重试耗尽 → 写入死信队列 + 标记失败
            try:
                from app.core.dlq import dlq
                dlq.push(
                    "embedding_requirement",
                    {"req_id": req_id, "srr_json_keys": list(srr_json.keys())[:10]},
                    str(exc)[:1500],
                    max_retries=self.max_retries,
                )
            except Exception:
                pass
            _update_vs_sync(req_id, "failed", str(exc)[:1500])

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_test_case",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
        # 修复 C4：TC 任务增加 time_limit + 重试耗尽后的 DLQ/状态兜底
        time_limit=180,
        soft_time_limit=150,
    )
    def embed_and_store_test_case(self, case_id: str, case_json: dict, metadata: dict):
        """异步计算嵌入并写入 Milvus（测试用例）。

        修复 C4：与需求任务对齐 —— 重试期间写 pending_retry；
        3 次失败则进 DLQ，并在 PostgreSQL.vectorization_status 写 failed，
        前端可诊断；避免永远 pending 的幽灵任务。

        Args:
            case_id: 用例 ID
            case_json: 用例 JSON
            metadata: Milvus 元数据
        """
        try:
            from app.db.milvus_store import vector_store
            vector_store.add_test_case(case_id, case_json, metadata=metadata)
            logger.info("[Celery] 嵌入+Milvus写入成功: case_id={}", case_id)
            _update_tc_vs_sync(case_id, "completed")
        except Exception as exc:
            logger.error(
                "[Celery] 嵌入+Milvus写入失败: case_id={}, error={}",
                case_id, str(exc)[:200],
            )
            if self.request.retries < self.max_retries:
                _update_tc_vs_sync(case_id, "pending_retry", str(exc)[:500])
                raise self.retry(exc=exc)
            # 修复 C4：重试耗尽写入 DLQ + 标记失败（与需求任务一致）
            try:
                from app.core.dlq import dlq
                dlq.push(
                    "embedding_test_case",
                    {
                        "case_id": case_id,
                        "req_id": metadata.get("req_id") if isinstance(metadata, dict) else None,
                        "case_keys": list(case_json.keys())[:10],
                    },
                    str(exc)[:1500],
                    max_retries=self.max_retries,
                )
            except Exception:
                pass
            _update_tc_vs_sync(case_id, "failed", str(exc)[:1500])
