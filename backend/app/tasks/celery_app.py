"""Celery 应用实例 — v4.1.9 起**已无内置任务**（向量化全部改走出箱）。

历史：本模块承载「PG 落库 → Celery 异步 Embedding → Milvus 写入」的异步双写。
v4.1.9 起归档/补写路径改为 `vector_outbox` 同事务入箱 + API 进程内 relay 投递，
`embedding_tasks` 两个任务已删除。保留本模块只为让 `celery_worker` 容器仍能启动
（服务空转一版观察，下一版删容器与 broker 配置）。

**新增后台任务时**：请先确认是否真的需要独立进程（CPU 隔离），
否则优先走 main.py lifespan 的周期协程（kb_lint / kb_maintenance / outbox relay 先例）。
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
        # v4.1.9：无内置任务（embedding_tasks 已删除，向量化改出箱 relay）
        include=[],
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
        # v4.1.9：task_routes 随任务删除一并清空（无路由可配）
        task_routes={},
    )
    logger.info("[Celery] 应用已初始化（无内置任务）: broker={}", broker)

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
