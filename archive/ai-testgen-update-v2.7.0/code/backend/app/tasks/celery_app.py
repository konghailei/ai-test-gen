"""Celery 应用实例 — 异步嵌入计算 + 知识库双写。

设计依据：
- docs/02-knowledge-base.md §9.7（异步双写一致性方案）
- PG 同步写入（事务保证）→ Celery 异步计算 Embedding → 写入 Milvus

降级策略：
- Celery 未安装或 broker 不可用 → celery_app = None → 归档端点降级为同步写入
"""
from __future__ import annotations

from app.config import settings
from app.utils.logger import logger

try:
    from celery import Celery

    broker = settings.CELERY_BROKER_URL or "redis://localhost:6379/1"
    backend = settings.CELERY_RESULT_BACKEND or "redis://localhost:6379/2"

    celery_app = Celery(
        "ai_test_gen",
        broker=broker,
        backend=backend,
        include=["app.tasks.embedding_tasks"],
    )

    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_default_queue="embedding",
        task_routes={
            "app.tasks.embedding_tasks.embed_and_store_requirement": {"queue": "embedding"},
            "app.tasks.embedding_tasks.embed_and_store_test_case": {"queue": "embedding"},
        },
    )
    logger.info("[Celery] 应用已初始化: broker={}", broker)
except ImportError:
    logger.warning("[Celery] celery 未安装，异步双写降级为同步")
    celery_app = None
