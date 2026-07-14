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
    """异步回写向量化状态到 PostgreSQL。"""
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


def _update_vs_sync(req_id: str, status: str, error: str | None = None):
    """同步封装：Celery 任务中调用 asyncio.run 来更新 DB。"""
    try:
        asyncio.run(_update_vectorization_status(req_id, status, error))
    except Exception as e:
        logger.warning("[Celery] 回写向量化状态失败: req_id={}, status={}, error={}", req_id, status, str(e)[:200])


if celery_app is not None:

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_requirement",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
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
            # 非最后一次重试 → 状态依然是 processing，等下次重试
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            # 全部重试耗尽 → 标记失败
            _update_vs_sync(req_id, "failed", str(exc)[:500])

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_test_case",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
    )
    def embed_and_store_test_case(self, case_id: str, case_json: dict, metadata: dict):
        """异步计算嵌入并写入 Milvus（测试用例）。

        Args:
            case_id: 用例 ID
            case_json: 用例 JSON
            metadata: Milvus 元数据
        """
        try:
            from app.db.milvus_store import vector_store
            vector_store.add_test_case(case_id, case_json, metadata=metadata)
            logger.info("[Celery] 嵌入+Milvus写入成功: case_id={}", case_id)
        except Exception as exc:
            logger.error(
                "[Celery] 嵌入+Milvus写入失败: case_id={}, error={}",
                case_id, str(exc)[:200],
            )
            raise self.retry(exc=exc)
