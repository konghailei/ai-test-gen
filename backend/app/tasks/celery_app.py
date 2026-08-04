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
        # ---- 修复 C4：全局超时与资源保护 ----
        # 单次 embedding + Milvus 写入不应超过 3 分钟（worker 进程硬上限）
        task_time_limit=180,
        # 软超时：150 秒触发 SoftTimeLimitExceeded，任务内部可捕获做 DLQ 收尾
        task_soft_time_limit=150,
        # 子进程级内存保护：每处理 500 个任务重启一次 worker（防止 embedding 模型内存泄漏）
        worker_max_tasks_per_child=500,
        # 单位 KB（2GB 上限）；防止单个 worker 无限制吃内存导致整机卡死
        worker_max_memory_per_child=2_000_000,
        # 结果最长保留 24h（防止 result backend Redis 内存爆炸）
        result_expires=86400,
        task_routes={
            "app.tasks.embedding_tasks.embed_and_store_requirement": {"queue": "embedding"},
            "app.tasks.embedding_tasks.embed_and_store_test_case": {"queue": "embedding"},
        },
    )
    logger.info("[Celery] 应用已初始化: broker={}", broker)
except ImportError:
    logger.warning("[Celery] celery 未安装，异步双写降级为同步")
    celery_app = None
