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


class _CeleryDisabled(Exception):
    """Celery 未启用（未安装或未配置 broker）时用于跳出初始化。"""


try:
    from celery import Celery

    # v3.4.1 修复 M-11：broker 未配置时直接走同步降级，不再用 localhost 兜底。
    # 原实现的 `or "redis://localhost:6379/1"` 使"未配 broker 即降级"这条分支不可达
    # （celery_app is None 只在 ImportError 时出现）；若本机恰好有 Redis，send_task
    # 会静默入队且无人消费，任务永久 pending —— 比直接失败更难排查。
    if not settings.CELERY_BROKER_URL:
        logger.warning("[Celery] CELERY_BROKER_URL 未配置，异步双写降级为同步执行")
        raise _CeleryDisabled()
    broker = settings.CELERY_BROKER_URL
    backend = settings.CELERY_RESULT_BACKEND or broker

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

    # ---- v4.1.5：worker 侧也必须显式设 torch 线程 ----
    # 此前只有 API 主进程设了（main.py 的 lifespan），**celery worker 完全没设**。
    # 后果：worker 进程用 torch 默认线程数（= 物理核数，与容器 cpus 配额无关），
    # 而部署脚本给 celery 的 CPU 只是 api 的一半 → 它按宿主核数开线程，
    # 直接抢走生成链路的物理核（表现为"生成忽然变慢，日志里什么都看不到"）。
    #
    # 为什么放在模块导入期而不是 worker 信号里：
    #   torch.set_num_threads 必须在**第一个张量运算之前**生效，而
    #   prefork 模式的子进程是 fork 出来的，父进程导入本模块时设置一次即可
    #   被子进程继承；放到 worker_ready 信号里反而可能晚于模型加载。
    # 失败不影响启动（无 torch 的环境照样能消费非 embedding 任务）。
    try:
        from app.core.resource_guard import apply_torch_threads

        apply_torch_threads()
    except Exception as _e:  # noqa: BLE001 - 线程设置失败不阻断 worker 启动
        logger.warning("[Celery] torch 线程设置失败（沿用默认值）: {}", str(_e)[:120])
except (ImportError, _CeleryDisabled):
    # v3.4.1 修复 M-11：未安装或未配置 broker 都走同步降级（celery_app = None）
    logger.warning("[Celery] celery 未安装或 broker 未配置，异步双写降级为同步")
    celery_app = None
