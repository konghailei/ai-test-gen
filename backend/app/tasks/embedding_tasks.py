"""嵌入计算 + Milvus 写入异步任务。

由归档端点触发：PG commit → celery_app.send_task → Milvus 写入
失败重试 3 次，最终失败记录到日志（不阻断归档）。

设计依据：
- docs/02-knowledge-base.md §9.7（异步双写一致性方案）
"""
from __future__ import annotations

from app.tasks.celery_app import celery_app
from app.utils.logger import logger


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
        except Exception as exc:
            logger.error(
                "[Celery] 嵌入+Milvus写入失败: req_id={}, error={}",
                req_id, str(exc)[:200],
            )
            raise self.retry(exc=exc)

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
